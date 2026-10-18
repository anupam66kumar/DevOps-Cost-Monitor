import os
import json
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Configurable Alerting Thresholds
BUDGET_LIMIT_INR = float(os.getenv("ALERT_BUDGET_LIMIT_INR", 35000.0))
MAX_DAILY_BURN_INR = float(os.getenv("ALERT_MAX_DAILY_BURN_INR", 1500.0))
WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")  # Optional: Slack, Teams, or Discord Webhook

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "costmonitor"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgrespassword")
    )

def check_and_send_alerts():
    print("\n" + "="*60)
    print("[*] Sprint 5: Multi-Cloud Cost & Anomaly Alert Engine")
    print("="*60)

    conn = get_db_connection()
    cur = conn.cursor()

    # 1. Check Total Multi-Cloud Spend MTD
    cur.execute("""
        SELECT COALESCE(sum(cost), 0) FROM (
            SELECT DISTINCT ON (provider) cost 
            FROM cloud_cost_snapshots 
            ORDER BY provider, collected_at DESC
        ) latest;
    """)
    total_spend = float(cur.fetchone()[0])

    # 2. Check 7-Day Average Daily Burn Rate
    cur.execute("""
        SELECT round(COALESCE(avg(daily_cost), 0), 2) FROM (
            SELECT usage_date, sum(daily_cost) as daily_cost 
            FROM cloud_daily_cost_history 
            GROUP BY usage_date 
            ORDER BY usage_date DESC 
            LIMIT 7
        ) recent;
    """)
    avg_burn = float(cur.fetchone()[0])

    # 3. Check for Idle or Cost-Heavy Resources in Inventory
    cur.execute("""
        SELECT provider, resource_type, count, size_gb, estimated_cost
        FROM cloud_resource_inventory
        WHERE collected_at = (SELECT max(collected_at) FROM cloud_resource_inventory)
        ORDER BY estimated_cost DESC;
    """)
    inventory_items = cur.fetchall()

    cur.close()
    conn.close()

    alerts = []

    # Rule 1: Monthly Budget Threshold Breach
    if total_spend > BUDGET_LIMIT_INR:
        alerts.append({
            "severity": "CRITICAL",
            "rule": "Monthly Budget Limit Exceeded",
            "metric": f"₹{total_spend:,.2f}",
            "threshold": f"₹{BUDGET_LIMIT_INR:,.2f}",
            "message": f"Total Multi-Cloud Spend MTD is ₹{total_spend:,.2f}, which has breached the defined limit of ₹{BUDGET_LIMIT_INR:,.2f}!",
            "action": "Review running Azure and GCP virtual machines. Consider rightsizing or scheduling auto-shutdown."
        })

    # Rule 2: High Daily Burn Rate Anomaly
    if avg_burn > MAX_DAILY_BURN_INR:
        alerts.append({
            "severity": "WARNING",
            "rule": "High Daily Burn Velocity",
            "metric": f"₹{avg_burn:,.2f}/day",
            "threshold": f"₹{MAX_DAILY_BURN_INR:,.2f}/day",
            "message": f"7-day average daily burn rate is ₹{avg_burn:,.2f}/day (Limit: ₹{MAX_DAILY_BURN_INR:,.2f}/day).",
            "action": "Inspect daily usage anomalies and evaluate unattached persistent disks or high egress traffic."
        })

    # Summary Display
    print(f"\n[+] Active Multi-Cloud Spend : ₹{total_spend:,.2f}")
    print(f"[+] 7-Day Burn Velocity     : ₹{avg_burn:,.2f}/day")
    print(f"[+] Monitored Resources      : {len(inventory_items)} inventory categories tracked")

    if not alerts:
        print("\n[✓] All cloud metrics are healthy and within operational thresholds.")
        return

    print(f"\n[!] Triggered {len(alerts)} Alert Notification(s):")
    for alert in alerts:
        print(f"\n>>> [{alert['severity']}] {alert['rule']}")
        print(f"    Current Value : {alert['metric']} (Threshold: {alert['threshold']})")
        print(f"    Message       : {alert['message']}")
        print(f"    Action Item   : {alert['action']}")

    # Webhook Notification Delivery (Slack / Teams / Discord)
    if WEBHOOK_URL:
        try:
            import requests
            payload = {
                "text": f"*Multi-Cloud FinOps Alert Notification*\n" + "\n".join(
                    [f"• *[{a['severity']}] {a['rule']}*: {a['message']}" for a in alerts]
                )
            }
            requests.post(WEBHOOK_URL, json=payload, timeout=5)
            print("\n[+] Webhook alert delivered successfully.")
        except Exception as e:
            print(f"\n[-] Webhook notification failed: {e}")

if __name__ == "__main__":
    check_and_send_alerts()
