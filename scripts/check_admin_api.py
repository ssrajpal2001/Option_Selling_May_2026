import requests

def get_admin_data():
    login_url = "http://localhost:5000/api/auth/login"
    login_data = {"username": "admin", "password": "Admin@123"}

    session = requests.Session()
    resp = session.post(login_url, json=login_data)
    print(f"Login Response: {resp.status_code}")

    if resp.status_code == 200:
        providers_url = "http://localhost:5000/api/admin/data-providers"
        resp = session.get(providers_url)
        print(f"Providers API: {resp.status_code}")
        print(resp.json())

if __name__ == "__main__":
    get_admin_data()
