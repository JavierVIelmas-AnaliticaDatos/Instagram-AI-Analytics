"""
02_extraer_posts.py

Extrae todas las publicaciones (fotos, videos, carruseles, reels) de una cuenta
de Instagram Business, obtiene sus métricas (insights) y las guarda en Postgres (Neon).

Requiere en .env / Streamlit secrets:
    ACCESS_TOKEN
    INSTAGRAM_ACCOUNT_ID
    NEON_DB_URL
"""

import requests
from datetime import datetime, timezone
from sqlalchemy import create_engine, text

from config import ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID, NEON_DB_URL

GRAPH_API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Métricas disponibles según el tipo de publicación.
# Meta cambia esto de vez en cuando -> si ves errores de "metric not supported",
# revisa la documentación oficial de Instagram Insights.
METRICS_BY_TYPE = {
    "IMAGE": ["reach", "saved", "likes", "comments", "shares", "total_interactions"],
    "CAROUSEL_ALBUM": ["reach", "saved", "likes", "comments", "shares", "total_interactions"],
    "VIDEO": ["reach", "saved", "likes", "comments", "shares", "total_interactions", "views"],
    "REEL": ["reach", "saved", "likes", "comments", "shares", "total_interactions", "views"],
}


def get_engine():
    if not NEON_DB_URL:
        raise ValueError("❌ NEON_DB_URL no está configurado en .env")
    return create_engine(NEON_DB_URL)


def crear_tabla_si_no_existe(engine):
    ddl = """
    CREATE TABLE IF NOT EXISTS publicaciones_instagram (
        media_id            TEXT PRIMARY KEY,
        cuenta_id            TEXT,
        media_type           TEXT,
        caption              TEXT,
        permalink            TEXT,
        timestamp_publicacion TIMESTAMPTZ,
        like_count           INTEGER,
        comments_count        INTEGER,
        reach                 INTEGER,
        saved                 INTEGER,
        shares                INTEGER,
        total_interactions    INTEGER,
        views                 INTEGER,
        fecha_extraccion     TIMESTAMPTZ DEFAULT now()
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
    print("✅ Tabla 'publicaciones_instagram' lista.")


def obtener_publicaciones():
    """Trae todas las publicaciones de la cuenta, siguiendo la paginación."""
    publicaciones = []
    url = (
        f"{BASE_URL}/{INSTAGRAM_ACCOUNT_ID}/media"
        f"?fields=id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count"
        f"&access_token={ACCESS_TOKEN}"
    )

    while url:
        response = requests.get(url)
        data = response.json()

        if "error" in data:
            print("❌ Error al obtener publicaciones:", data["error"]["message"])
            break

        publicaciones.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")  # None cuando no hay más páginas

    return publicaciones


def obtener_insights(media_id, media_type, media_product_type):
    """Obtiene las métricas de un post según su tipo."""
    # Instagram distingue REEL vía media_product_type, no solo media_type
    tipo_clave = "REEL" if media_product_type == "REELS" else media_type
    metricas = METRICS_BY_TYPE.get(tipo_clave)

    if not metricas:
        return {}

    url = (
        f"{BASE_URL}/{media_id}/insights"
        f"?metric={','.join(metricas)}"
        f"&access_token={ACCESS_TOKEN}"
    )
    response = requests.get(url)
    data = response.json()

    if "error" in data:
        print(f"   ⚠️ No se pudieron obtener insights de {media_id}: {data['error']['message']}")
        return {}

    resultado = {}
    for item in data.get("data", []):
        nombre = item["name"]
        valores = item.get("values", [])
        if valores:
            resultado[nombre] = valores[0].get("value", 0)

    return resultado


def guardar_publicacion(engine, post, insights):
    upsert_sql = """
    INSERT INTO publicaciones_instagram (
        media_id, cuenta_id, media_type, caption, permalink,
        timestamp_publicacion, like_count, comments_count,
        reach, saved, shares, total_interactions, views
    ) VALUES (
        :media_id, :cuenta_id, :media_type, :caption, :permalink,
        :timestamp_publicacion, :like_count, :comments_count,
        :reach, :saved, :shares, :total_interactions, :views
    )
    ON CONFLICT (media_id) DO UPDATE SET
        like_count = EXCLUDED.like_count,
        comments_count = EXCLUDED.comments_count,
        reach = EXCLUDED.reach,
        saved = EXCLUDED.saved,
        shares = EXCLUDED.shares,
        total_interactions = EXCLUDED.total_interactions,
        views = EXCLUDED.views,
        fecha_extraccion = now();
    """

    with engine.begin() as conn:
        conn.execute(text(upsert_sql), {
            "media_id": post["id"],
            "cuenta_id": INSTAGRAM_ACCOUNT_ID,
            "media_type": post.get("media_type"),
            "caption": post.get("caption"),
            "permalink": post.get("permalink"),
            "timestamp_publicacion": post.get("timestamp"),
            "like_count": post.get("like_count", 0),
            "comments_count": post.get("comments_count", 0),
            "reach": insights.get("reach"),
            "saved": insights.get("saved"),
            "shares": insights.get("shares"),
            "total_interactions": insights.get("total_interactions"),
            "views": insights.get("views"),
        })


def main():
    if not ACCESS_TOKEN or not INSTAGRAM_ACCOUNT_ID:
        print("❌ Falta ACCESS_TOKEN o INSTAGRAM_ACCOUNT_ID en .env")
        return

    print("🔌 Conectando a la base de datos...")
    engine = get_engine()
    crear_tabla_si_no_existe(engine)

    print("📥 Descargando publicaciones de Instagram...")
    publicaciones = obtener_publicaciones()
    print(f"✅ {len(publicaciones)} publicaciones encontradas.\n")

    if not publicaciones:
        print("⚠️ No hay publicaciones que procesar (cuenta vacía).")
        return

    for i, post in enumerate(publicaciones, start=1):
        print(f"[{i}/{len(publicaciones)}] Procesando post {post['id']} ({post.get('media_type')})...")
        insights = obtener_insights(
            post["id"],
            post.get("media_type"),
            post.get("media_product_type"),
        )
        guardar_publicacion(engine, post, insights)

    print("\n✅ Extracción completa. Datos guardados en 'publicaciones_instagram'.")


if __name__ == "__main__":
    main()