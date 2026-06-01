"""
Diagnostic script to inspect the CapriQuant database.
Run this from your python-backend folder.
"""

import os
from dotenv import load_dotenv
import psycopg2
from psycopg2 import sql

load_dotenv()

def main():
    print("Connecting to database...")
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT")
        )
        cursor = conn.cursor()
        print("Connected successfully!\n")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    # 1. List all tables in the public schema
    print("=" * 60)
    print("TABLES IN DATABASE (public schema)")
    print("=" * 60)
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        ORDER BY table_name;
    """)
    tables = cursor.fetchall()

    if not tables:
        print("No tables found in public schema.")
        return

    for (table_name,) in tables:
        print(f" - {table_name}")

    print("\n" + "=" * 60)

    # 2. Show detailed schema for each table
    for (table_name,) in tables:
        print(f"\n\nTABLE: {table_name}")
        print("-" * 60)

        # Column information
        cursor.execute("""
            SELECT 
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns 
            WHERE table_schema = 'public' 
              AND table_name = %s
            ORDER BY ordinal_position;
        """, (table_name,))

        columns = cursor.fetchall()
        print("Columns:")
        for col in columns:
            col_name, data_type, nullable, default = col
            default_str = f" DEFAULT {default}" if default else ""
            null_str = "NULL" if nullable == "YES" else "NOT NULL"
            print(f"  {col_name:20} {data_type:15} {null_str:8}{default_str}")

        # Row count
        cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table_name)))
        count = cursor.fetchone()[0]
        print(f"\nTotal rows: {count:,}")

        # Sample data (last 5 rows if possible)
        if count > 0:
            print("\nSample data (most recent 5 rows if timestamp exists):")
            try:
                cursor.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY timestamp DESC LIMIT 5").format(
                        sql.Identifier(table_name)
                    )
                )
                rows = cursor.fetchall()
                colnames = [desc[0] for desc in cursor.description]
                print(" | ".join(colnames))
                print("-" * 80)
                for row in rows:
                    print(" | ".join(str(x)[:20] for x in row))
            except Exception:
                # Fallback: just get any 5 rows
                cursor.execute(sql.SQL("SELECT * FROM {} LIMIT 5").format(sql.Identifier(table_name)))
                rows = cursor.fetchall()
                colnames = [desc[0] for desc in cursor.description]
                print(" | ".join(colnames))
                print("-" * 80)
                for row in rows:
                    print(" | ".join(str(x)[:20] for x in row))

    cursor.close()
    conn.close()
    print("\n\nInspection complete.")

if __name__ == "__main__":
    main()
