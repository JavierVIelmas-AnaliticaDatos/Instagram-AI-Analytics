"""
03_analisis_engagement.py

Análisis exploratorio de las publicaciones de Instagram guardadas en Postgres (Neon).
Calcula métricas de engagement, compara tipos de contenido y detecta patrones
básicos (mejor día/hora de publicación, top posts, etc.).

Requiere en .env / Streamlit secrets:
    NEON_DB_URL

Uso:
    python3 03_analisis_engagement.py
"""

import pandas as pd
from sqlalchemy import create_engine

from config import NEON_DB_URL

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 140)


def cargar_datos():
    engine = create_engine(NEON_DB_URL)
    query = "SELECT * FROM publicaciones_instagram ORDER BY timestamp_publicacion;"
    df = pd.read_sql(query, engine)
    return df


def preparar_datos(df):
    """Limpieza y columnas derivadas."""
    df["timestamp_publicacion"] = pd.to_datetime(df["timestamp_publicacion"])
    df["dia_semana"] = df["timestamp_publicacion"].dt.day_name()
    df["hora_publicacion"] = df["timestamp_publicacion"].dt.hour

    # Rellenar nulos numéricos con 0 para poder operar (algunas métricas
    # no aplican a todos los tipos de contenido, ej. 'views' en IMAGE)
    columnas_numericas = [
        "like_count", "comments_count", "reach",
        "saved", "shares", "total_interactions", "views",
    ]
    for col in columnas_numericas:
        df[col] = df[col].fillna(0)

    # Tasa de engagement = interacciones totales / alcance (reach)
    # Evitamos división por cero
    df["engagement_rate_pct"] = df.apply(
        lambda row: round((row["total_interactions"] / row["reach"]) * 100, 2)
        if row["reach"] > 0 else 0,
        axis=1,
    )

    return df


def resumen_general(df):
    print("=" * 60)
    print("📊 RESUMEN GENERAL")
    print("=" * 60)
    print(f"Total de publicaciones: {len(df)}")
    print(f"Rango de fechas: {df['timestamp_publicacion'].min().date()} → {df['timestamp_publicacion'].max().date()}")
    print()

    metricas = ["like_count", "comments_count", "reach", "saved", "shares", "views", "engagement_rate_pct"]
    print(df[metricas].describe().round(2))
    print()


def analisis_por_tipo(df):
    print("=" * 60)
    print("📁 DESEMPEÑO POR TIPO DE CONTENIDO")
    print("=" * 60)
    resumen = df.groupby("media_type").agg(
        publicaciones=("media_id", "count"),
        promedio_likes=("like_count", "mean"),
        promedio_comentarios=("comments_count", "mean"),
        promedio_reach=("reach", "mean"),
        promedio_engagement_pct=("engagement_rate_pct", "mean"),
    ).round(2)
    print(resumen)
    print()


def mejores_publicaciones(df, top_n=5):
    print("=" * 60)
    print(f"🏆 TOP {top_n} PUBLICACIONES POR ENGAGEMENT")
    print("=" * 60)
    top = df.sort_values("engagement_rate_pct", ascending=False).head(top_n)
    print(top[["media_id", "media_type", "like_count", "comments_count", "reach", "engagement_rate_pct", "permalink"]])
    print()


def patrones_publicacion(df):
    print("=" * 60)
    print("🕒 PATRONES DE PUBLICACIÓN")
    print("=" * 60)

    por_dia = df.groupby("dia_semana")["engagement_rate_pct"].mean().round(2).sort_values(ascending=False)
    print("Engagement promedio por día de la semana:")
    print(por_dia)
    print()

    por_hora = df.groupby("hora_publicacion")["engagement_rate_pct"].mean().round(2).sort_values(ascending=False)
    print("Engagement promedio por hora de publicación (UTC):")
    print(por_hora)
    print()


def exportar_csv(df, path="analisis_publicaciones.csv"):
    df.to_csv(path, index=False)
    print(f"💾 Datos exportados a '{path}'")


def main():
    print("📥 Cargando datos desde la base de datos...\n")
    df = cargar_datos()

    if df.empty:
        print("⚠️ No hay publicaciones en la tabla todavía. Corre primero 02_extraer_posts.py")
        return

    df = preparar_datos(df)

    resumen_general(df)
    analisis_por_tipo(df)
    mejores_publicaciones(df)
    patrones_publicacion(df)
    exportar_csv(df)


if __name__ == "__main__":
    main()