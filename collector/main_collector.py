import os
import time
import json
import psycopg2
from psycopg2.extras import Json
from datetime import datetime, timezone
from dotenv import load_dotenv

# Cloud SDKs
from azure.identity import AzureCliCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryDefinition,
    QueryDataset,
    QueryAggregation,
)
from google.cloud import compute_v1, storage
import boto3

load_dotenv()

class MultiRegionCloudCollector:
    def __init__(self):
        self.azure_sub_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        self.gcp_project = os.getenv("GCP_PROJECT_ID", "heroviredacademics")
        self.minio_endpoint = os.getenv("AWS_ENDPOINT_URL", "http://localhost:9000")
        self.minio_bucket = os.getenv("MINIO_BUCKET_NAME", "cost-monitor-data")
        self.collected_at = datetime.now(timezone.utc)

        # Database Configuration
        self.db_host = os.getenv("DB_HOST", "localhost")
        self.db_port = os.getenv("DB_PORT", "5432")
        self.db_name = os.getenv("DB_NAME", "costmonitor")
        self.db_user = os.getenv("DB_USER", "postgres")
        self.db_pass = os.getenv("DB_PASSWORD", "postgrespassword")

    def get_db_connection(self):
        return psycopg2.connect(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_pass
        )

    def init_db(self):
        """Ensures all necessary tables and indexes exist in PostgreSQL"""
        schema_sql = """
        CREATE TABLE IF NOT EXISTS cloud_cost_snapshots (
            id SERIAL PRIMARY KEY,
            collected_at TIMESTAMPTZ NOT NULL,
            provider VARCHAR(50) NOT NULL,
            scope VARCHAR(100) NOT NULL,
            region VARCHAR(50) NOT NULL,
            metric_type VARCHAR(100) NOT NULL,
            cost NUMERIC(12, 2) NOT NULL,
            currency VARCHAR(10) NOT NULL,
            status VARCHAR(20) NOT NULL,
            raw_details JSONB
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_provider ON cloud_cost_snapshots(provider);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON cloud_cost_snapshots(collected_at DESC);

        CREATE TABLE IF NOT EXISTS cloud_resource_inventory (
            id SERIAL PRIMARY KEY,
            snapshot_id INTEGER REFERENCES cloud_cost_snapshots(id) ON DELETE CASCADE,
            collected_at TIMESTAMPTZ NOT NULL,
            provider VARCHAR(50) NOT NULL,
            resource_type VARCHAR(50) NOT NULL,
            count INTEGER NOT NULL,
            size_gb NUMERIC(12, 2) DEFAULT 0.0,
            estimated_cost NUMERIC(12, 2) DEFAULT 0.0
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_provider ON cloud_resource_inventory(provider);
        CREATE INDEX IF NOT EXISTS idx_inventory_time ON cloud_resource_inventory(collected_at DESC);

        -- Sprint 4: Table for Normalized Daily Cost History & Trend Analytics
        CREATE TABLE IF NOT EXISTS cloud_daily_cost_history (
            id SERIAL PRIMARY KEY,
            provider VARCHAR(50) NOT NULL,
            usage_date DATE NOT NULL,
            daily_cost NUMERIC(12, 2) NOT NULL,
            currency VARCHAR(10) NOT NULL DEFAULT 'INR',
            recorded_at TIMESTAMPTZ NOT NULL,
            UNIQUE(provider, usage_date)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_history ON cloud_daily_cost_history(provider, usage_date DESC);
        """
        conn = self.get_db_connection()
        cur = conn.cursor()
        cur.execute(schema_sql)
        conn.commit()
        cur.close()
        conn.close()

    def fetch_azure_global_costs(self, retries=3, delay=10):
        print("[*] Fetching Azure Cost Management Data across ALL global regions...")
        for attempt in range(1, retries + 1):
            try:
                client = CostManagementClient(AzureCliCredential())
                scope = f"/subscriptions/{self.azure_sub_id}"
                
                query = QueryDefinition(
                    type="ActualCost",
                    timeframe="MonthToDate",
                    dataset=QueryDataset(
                        granularity="Daily",
                        aggregation={
                            "totalCost": QueryAggregation(name="PreTaxCost", function="Sum")
                        },
                        grouping=[{
                            "type": "Dimension",
                            "name": "ResourceLocation"
                        }]
                    ),
                )
                result = client.query.usage(scope=scope, parameters=query)
                
                daily_breakdown = {}
                total_global_cost = 0.0

                for row in result.rows:
                    cost = float(row[0])
                    usage_date = str(row[1]) if len(row) > 1 and row[1] else "unknown"
                    total_global_cost += cost
                    daily_breakdown[usage_date] = daily_breakdown.get(usage_date, 0.0) + cost

                return {
                    "provider": "Azure",
                    "scope": self.azure_sub_id,
                    "region": "global",
                    "metric_type": "actual_cost_mtd",
                    "cost": round(total_global_cost, 2),
                    "currency": "INR",
                    "regional_breakdown": {k: round(v, 2) for k, v in daily_breakdown.items()},
                    "status": "SUCCESS"
                }
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and attempt < retries:
                    print(f"[-] Azure rate limit (429) hit. Backing off for {delay}s (Attempt {attempt}/{retries})...")
                    time.sleep(delay)
                    delay *= 2
                else:
                    return {"provider": "Azure", "status": "ERROR", "error": err_str}

    def fetch_gcp_global_resources(self):
        print("[*] Fetching GCP Resources across ALL global zones/regions...")
        try:
            # 1. Global VM Instances
            instance_client = compute_v1.InstancesClient()
            agg_instances = instance_client.aggregated_list(project=self.gcp_project)
            all_instances = []
            for zone, scoped_list in agg_instances:
                if scoped_list.instances:
                    all_instances.extend(scoped_list.instances)

            # 2. Global Persistent Disks
            disk_client = compute_v1.DisksClient()
            agg_disks = disk_client.aggregated_list(project=self.gcp_project)
            all_disks = []
            for zone, scoped_list in agg_disks:
                if scoped_list.disks:
                    all_disks.extend(scoped_list.disks)
            
            total_disk_gb = sum(disk.size_gb for disk in all_disks)

            # 3. Global Storage Buckets
            storage_client = storage.Client(project=self.gcp_project)
            all_buckets = list(storage_client.list_buckets())

            vm_count = len(all_instances)
            disk_count = len(all_disks)
            bucket_count = len(all_buckets)

            # Daily run-rate estimation: (~2250 / 30 = 75 INR/day per VM, ~3.5 / 30 = 0.117 INR/GB/day)
            daily_burn = round((vm_count * 75.0) + (total_disk_gb * 0.117), 2)
            monthly_est = round((vm_count * 2250.0) + (total_disk_gb * 3.5), 2)

            return {
                "provider": "GCP",
                "scope": self.gcp_project,
                "region": "global",
                "metric_type": "estimated_resource_cost_monthly",
                "details": {
                    "total_vms": vm_count,
                    "total_disks": disk_count,
                    "total_disk_gb": total_disk_gb,
                    "total_buckets": bucket_count,
                    "daily_burn_rate": daily_burn
                },
                "cost": monthly_est,
                "currency": "INR",
                "status": "SUCCESS"
            }
        except Exception as e:
            return {"provider": "GCP", "status": "ERROR", "error": str(e)}

    def fetch_minio_storage(self):
        print("[*] Fetching Local S3 / MinIO Metrics (ap-south-1)...")
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=self.minio_endpoint,
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
                region_name="ap-south-1"
            )

            try:
                s3.head_bucket(Bucket=self.minio_bucket)
            except Exception:
                s3.create_bucket(Bucket=self.minio_bucket)

            resp = s3.list_objects_v2(Bucket=self.minio_bucket)
            objects = resp.get("Contents", [])
            total_bytes = sum(obj["Size"] for obj in objects)
            total_gb = round(total_bytes / (1024 ** 3), 4)
            cost_inr = round(total_gb * 1.95, 4)

            return {
                "provider": "MinIO (Local S3)",
                "scope": self.minio_bucket,
                "region": "ap-south-1",
                "metric_type": "storage_utilization_cost",
                "details": {
                    "object_count": len(objects),
                    "size_bytes": total_bytes,
                    "size_gb": total_gb,
                    "daily_burn_rate": round(cost_inr / 30.0, 4)
                },
                "cost": cost_inr,
                "currency": "INR",
                "status": "SUCCESS"
            }
        except Exception as e:
            return {"provider": "MinIO", "status": "ERROR", "error": str(e)}

    def save_to_database(self, providers_data):
        print("\n[*] Persisting telemetry and historical time-series into PostgreSQL...")
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.cursor()

            for item in providers_data:
                if item.get("status") != "SUCCESS":
                    continue

                provider = item.get("provider")
                scope = item.get("scope", "default")
                region = item.get("region", "global")
                metric_type = item.get("metric_type", "cost")
                cost = item.get("cost", 0.0)
                currency = item.get("currency", "INR")
                status = item.get("status")
                raw_details = Json(item.get("details", item.get("regional_breakdown", {})))

                # 1. High-level snapshot
                cur.execute("""
                    INSERT INTO cloud_cost_snapshots 
                    (collected_at, provider, scope, region, metric_type, cost, currency, status, raw_details)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                """, (self.collected_at, provider, scope, region, metric_type, cost, currency, status, raw_details))
                snapshot_id = cur.fetchone()[0]

                # 2. Granular inventory
                if provider == "GCP" and "details" in item:
                    details = item["details"]
                    cur.execute("""
                        INSERT INTO cloud_resource_inventory (snapshot_id, collected_at, provider, resource_type, count, size_gb, estimated_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (snapshot_id, self.collected_at, "GCP", "virtual_machines", details.get("total_vms", 0), 0, round(details.get("total_vms", 0) * 2250.0, 2)))

                    cur.execute("""
                        INSERT INTO cloud_resource_inventory (snapshot_id, collected_at, provider, resource_type, count, size_gb, estimated_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (snapshot_id, self.collected_at, "GCP", "persistent_disks", details.get("total_disks", 0), details.get("total_disk_gb", 0), round(details.get("total_disk_gb", 0) * 3.5, 2)))

                    cur.execute("""
                        INSERT INTO cloud_resource_inventory (snapshot_id, collected_at, provider, resource_type, count, size_gb, estimated_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (snapshot_id, self.collected_at, "GCP", "storage_buckets", details.get("total_buckets", 0), 0, 0))

                    # Daily trend insertion for GCP
                    cur.execute("""
                        INSERT INTO cloud_daily_cost_history (provider, usage_date, daily_cost, currency, recorded_at)
                        VALUES (%s, CURRENT_DATE, %s, 'INR', %s)
                        ON CONFLICT (provider, usage_date) 
                        DO UPDATE SET daily_cost = EXCLUDED.daily_cost, recorded_at = EXCLUDED.recorded_at;
                    """, ("GCP", details.get("daily_burn_rate", 76.17), self.collected_at))

                elif provider == "MinIO (Local S3)" and "details" in item:
                    details = item["details"]
                    cur.execute("""
                        INSERT INTO cloud_resource_inventory (snapshot_id, collected_at, provider, resource_type, count, size_gb, estimated_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (snapshot_id, self.collected_at, "MinIO", "s3_buckets", 1, details.get("size_gb", 0), cost))

                    # Daily trend insertion for MinIO
                    cur.execute("""
                        INSERT INTO cloud_daily_cost_history (provider, usage_date, daily_cost, currency, recorded_at)
                        VALUES (%s, CURRENT_DATE, %s, 'INR', %s)
                        ON CONFLICT (provider, usage_date) 
                        DO UPDATE SET daily_cost = EXCLUDED.daily_cost, recorded_at = EXCLUDED.recorded_at;
                    """, ("MinIO", details.get("daily_burn_rate", 0.0), self.collected_at))

                # 3. Populate Azure Daily Cost History directly into time-series table
                elif provider == "Azure" and "regional_breakdown" in item:
                    for date_str, daily_cost in item["regional_breakdown"].items():
                        try:
                            # parse YYYYMMDD string format from Azure Cost Management
                            parsed_date = datetime.strptime(date_str, "%Y%m%d").date()
                            cur.execute("""
                                INSERT INTO cloud_daily_cost_history (provider, usage_date, daily_cost, currency, recorded_at)
                                VALUES (%s, %s, %s, 'INR', %s)
                                ON CONFLICT (provider, usage_date) 
                                DO UPDATE SET daily_cost = EXCLUDED.daily_cost, recorded_at = EXCLUDED.recorded_at;
                            """, ("Azure", parsed_date, daily_cost, self.collected_at))
                        except Exception as e:
                            pass

            conn.commit()
            cur.close()
            print("[+] Successfully persisted historical time-series data!")
        except Exception as e:
            print(f"[-] Database persistence failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    def run(self):
        self.init_db()
        providers = [
            self.fetch_azure_global_costs(),
            self.fetch_gcp_global_resources(),
            self.fetch_minio_storage()
        ]
        self.save_to_database(providers)
        return {"timestamp": self.collected_at.isoformat(), "providers": providers}

if __name__ == "__main__":
    collector = MultiRegionCloudCollector()
    collector.run()
