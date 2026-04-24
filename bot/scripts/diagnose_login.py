import sys
import os
import bcrypt
import subprocess
import argparse
from pathlib import Path

# Add the project root to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

def diagnose():
    # Import inside the function so that os.environ["ALGOSOFT_DB_PATH"] can take effect
    from web.auth import verify_password
    from web.db import db_fetchall, DB_PATH

    print("--- AlgoSoft Login Diagnostic Tool ---")
    print(f"Active Database Path (Targeted): {os.path.abspath(DB_PATH)}")

    # Check Environment Variables
    print("\n--- Environment Variables ---")
    found_env = False
    for k, v in os.environ.items():
        if k.startswith("ALGOSOFT"):
            # Obfuscate secret but keep path visible
            val = v if "SECRET" not in k else (v[:4] + "****" + v[-4:] if len(v) > 8 else "****")
            print(f"{k}: {val}")
            found_env = True
    if not found_env:
        print("No ALGOSOFT_* environment variables found.")

    # Search for other database files
    print("\n--- System Database Discovery ---")
    all_found_dbs = []
    try:
        # Search for any sqlite files named algosoft.db in common locations
        search_dirs = ["/home/ec2-user", "/app", os.getcwd()]
        found = []
        for d in set(search_dirs):
            if os.path.exists(d):
                res = subprocess.run(["find", d, "-name", "algosoft.db"], capture_output=True, text=True, timeout=10)
                found.extend(res.stdout.strip().split("\n"))

        all_found_dbs = sorted(list(set([os.path.abspath(f) for f in found if f])))
        if all_found_dbs:
            print(f"Found {len(all_found_dbs)} database file(s) on disk:")
            for f in all_found_dbs:
                mark = " (TARGETED)" if f == os.path.abspath(DB_PATH) else ""
                print(f" - {f}{mark}")
        else:
            print("No 'algosoft.db' files found in common directories.")
    except Exception as e:
        print(f"Database Search Failed: {str(e)}")

    # Check for running web server and its open files
    print("\n--- Running Process Analysis ---")
    try:
        # Find python processes running server.py
        ps_res = subprocess.run(["pgrep", "-af", "server.py"], capture_output=True, text=True)
        processes = ps_res.stdout.strip().split("\n")
        processes = [p for p in processes if p]

        if processes:
            print(f"Found {len(processes)} running server process(es):")
            for p in processes:
                print(f" - Process: {p}")
                pid = p.split()[0]
                # Check what files this process has open
                try:
                    lsof_res = subprocess.run(["lsof", "-p", pid], capture_output=True, text=True)
                    open_dbs = [line.split()[-1] for line in lsof_res.stdout.split("\n") if "algosoft.db" in line]
                    if open_dbs:
                        for odb in set(open_dbs):
                            print(f"   --> OPEN DATABASE: {os.path.abspath(odb)}")
                    else:
                        print("   --> No 'algosoft.db' file found in open file list for this PID.")
                except Exception:
                    print("   --> Could not check open files (try running with sudo).")
        else:
            print("No running 'server.py' processes found.")
    except Exception as e:
        print(f"Process Analysis Failed: {str(e)}")

    # Check bcrypt version
    print("\n--- Authentication Libraries ---")
    print(f"Bcrypt Version: {bcrypt.__version__}")

    # Test bcrypt functionality
    try:
        password = "test_password"
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        if bcrypt.checkpw(password.encode(), hashed):
            print("Bcrypt Test: SUCCESS")
        else:
            print("Bcrypt Test: FAILED (Verification failed)")
    except Exception as e:
        print(f"Bcrypt Test: ERROR ({str(e)})")

    # Check Database Users
    print("\n--- Database User Status (Targeted DB) ---")
    try:
        users = db_fetchall("SELECT username, role, is_active, password_hash FROM users")
        if not users:
            print("No users found in targeted database.")
        for u in users:
            status = "ACTIVE" if u['is_active'] else "INACTIVE"
            pw_hash_preview = u['password_hash'][:10] + "..."
            print(f"User: {u['username']:<15} | Role: {u['role']:<10} | Status: {status:<10} | Hash: {pw_hash_preview}")

            # Diagnostic check for common default passwords
            if u['username'] == 'admin':
                if verify_password('Admin@123', u['password_hash']):
                    print("  -> admin: DEFAULT PASSWORD VERIFIED")
                else:
                    print("  -> admin: Password verification failed (incorrect or incompatible hash)")
    except Exception as e:
        print(f"Database Error: {str(e)}")

    print("\n--- Recommendation ---")
    print("If your script shows different users than the web UI, you are likely looking at the wrong database.")
    print("Use the --db <path> flag to specify the correct database file.")
    print("\nExample to fix:")
    print("python3 scripts/factory_reset.py --db /path/to/correct/algosoft.db")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlgoSoft Diagnostic Tool")
    parser.add_argument("--db", help="Path to the algosoft.db file to diagnose")
    args = parser.parse_args()

    if args.db:
        os.environ["ALGOSOFT_DB_PATH"] = args.db

    diagnose()
