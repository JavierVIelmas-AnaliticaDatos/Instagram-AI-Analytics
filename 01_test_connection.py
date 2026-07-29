import requests
from config import ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID

def test_instagram_connection():
    if not ACCESS_TOKEN or not INSTAGRAM_ACCOUNT_ID:
        print("❌ Error: Falta ACCESS_TOKEN o INSTAGRAM_ACCOUNT_ID en .env")
        return

    url = f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}?fields=username,name,followers_count,media_count&access_token={ACCESS_TOKEN}"
    response = requests.get(url)
    data = response.json()

    if "error" in data:
        print("❌ Error en la API de Meta:", data["error"]["message"])
        return

    print("✅ ¡Conexión con Instagram Business exitosa!")
    print(f"👤 Usuario: @{data.get('username')}")
    print(f"📛 Nombre: {data.get('name')}")
    print(f"👥 Seguidores: {data.get('followers_count')}")
    print(f"📸 Posts: {data.get('media_count')}")

if __name__ == "__main__":
    test_instagram_connection()