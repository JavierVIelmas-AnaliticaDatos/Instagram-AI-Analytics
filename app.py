"""
app.py

Dashboard de analítica de Instagram: visualiza engagement, alcance y
desempeño por tipo de contenido a partir de los datos guardados en Postgres (Neon).

Requiere en .env / Streamlit secrets:
    NEON_DB_URL

Uso local:
    streamlit run app.py
"""

import pandas as pd
import plotly.express as px
import streamlit as st
from sqlalchemy import create_engine

from config import NEON_DB_URL

# --- DEBUG TEMPORAL: quitar después de resolver el problema ---
st.write("DEBUG NEON_DB_URL es None:", NEON_DB_URL is None)
st.write("DEBUG tipo:", type(NEON_DB_URL))
if NEON_DB_URL:
    st.write("DEBUG primeros 15 caracteres:", NEON_DB_URL[:15])
# --- FIN DEBUG ---

st.set_page_config(
    page_title="Instagram Analytics",
    page_icon="📸",
    layout="wide",
)


@st.cache_resource
def get_engine():
    return create_engine(NEON_DB_URL)


@st.cache_data(ttl=300)  # refresca cada 5 minutos
def cargar_datos():
    engine = get_engine()
    query = "SELECT * FROM publicaciones_instagram ORDER BY timestamp_publicacion;"
    df = pd.read_sql(query, engine)
    return df


def preparar_datos(df):
    df["timestamp_publicacion"] = pd.to_datetime(df["timestamp_publicacion"])
    df["fecha"] = df["timestamp_publicacion"].dt.date
    df["dia_semana"] = df["timestamp_publicacion"].dt.day_name()
    df["hora_publicacion"] = df["timestamp_publicacion"].dt.hour

    columnas_numericas = [
        "like_count", "comments_count", "reach",
        "saved", "shares", "total_interactions", "views",
    ]
    for col in columnas_numericas:
        df[col] = df[col].fillna(0)

    df["engagement_rate_pct"] = df.apply(
        lambda row: round((row["total_interactions"] / row["reach"]) * 100, 2)
        if row["reach"] > 0 else 0,
        axis=1,
    )

    return df


def main():
    st.title("📸 Instagram Analytics Dashboard")
    st.caption("Análisis de interacción y desempeño de publicaciones")

    df = cargar_datos()

    if df.empty:
        st.warning("⚠️ No hay publicaciones en la base de datos todavía. Corre `02_extraer_posts.py` primero.")
        return

    df = preparar_datos(df)

    # ---- Filtros en la barra lateral ----
    st.sidebar.header("Filtros")

    tipos_disponibles = sorted(df["media_type"].unique().tolist())
    tipos_seleccionados = st.sidebar.multiselect(
        "Tipo de contenido", tipos_disponibles, default=tipos_disponibles
    )

    fecha_min = df["fecha"].min()
    fecha_max = df["fecha"].max()
    rango_fechas = st.sidebar.date_input(
        "Rango de fechas", value=(fecha_min, fecha_max), min_value=fecha_min, max_value=fecha_max
    )

    df_filtrado = df[df["media_type"].isin(tipos_seleccionados)]
    if isinstance(rango_fechas, tuple) and len(rango_fechas) == 2:
        inicio, fin = rango_fechas
        df_filtrado = df_filtrado[(df_filtrado["fecha"] >= inicio) & (df_filtrado["fecha"] <= fin)]

    if df_filtrado.empty:
        st.info("No hay publicaciones que coincidan con los filtros seleccionados.")
        return

    # ---- KPIs principales ----
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Publicaciones", len(df_filtrado))
    col2.metric("Likes promedio", f"{df_filtrado['like_count'].mean():.1f}")
    col3.metric("Comentarios promedio", f"{df_filtrado['comments_count'].mean():.1f}")
    col4.metric("Reach promedio", f"{df_filtrado['reach'].mean():.1f}")
    col5.metric("Engagement promedio", f"{df_filtrado['engagement_rate_pct'].mean():.1f}%")

    st.divider()

    # ---- Gráfico: evolución de engagement en el tiempo ----
    st.subheader("Evolución del engagement en el tiempo")
    fig_evolucion = px.line(
        df_filtrado.sort_values("timestamp_publicacion"),
        x="timestamp_publicacion",
        y="engagement_rate_pct",
        markers=True,
        color="media_type",
        labels={"timestamp_publicacion": "Fecha", "engagement_rate_pct": "Engagement (%)", "media_type": "Tipo"},
    )
    st.plotly_chart(fig_evolucion, width="stretch")

    # ---- Dos columnas: desempeño por tipo + reach vs interacciones ----
    col_izq, col_der = st.columns(2)

    with col_izq:
        st.subheader("Engagement promedio por tipo de contenido")
        resumen_tipo = df_filtrado.groupby("media_type", as_index=False)["engagement_rate_pct"].mean()
        fig_tipo = px.bar(
            resumen_tipo,
            x="media_type",
            y="engagement_rate_pct",
            color="media_type",
            labels={"media_type": "Tipo de contenido", "engagement_rate_pct": "Engagement (%)"},
        )
        st.plotly_chart(fig_tipo, width="stretch")

    with col_der:
        st.subheader("Alcance vs. Interacciones totales")
        fig_scatter = px.scatter(
            df_filtrado,
            x="reach",
            y="total_interactions",
            color="media_type",
            size="like_count",
            hover_data=["permalink"],
            labels={"reach": "Alcance (reach)", "total_interactions": "Interacciones totales"},
        )
        st.plotly_chart(fig_scatter, width="stretch")

    # ---- Patrones de publicación ----
    st.subheader("Patrones de publicación")
    col_dia, col_hora = st.columns(2)

    with col_dia:
        por_dia = df_filtrado.groupby("dia_semana", as_index=False)["engagement_rate_pct"].mean()
        fig_dia = px.bar(
            por_dia, x="dia_semana", y="engagement_rate_pct",
            labels={"dia_semana": "Día de la semana", "engagement_rate_pct": "Engagement (%)"},
        )
        st.plotly_chart(fig_dia, width="stretch")

    with col_hora:
        por_hora = df_filtrado.groupby("hora_publicacion", as_index=False)["engagement_rate_pct"].mean()
        fig_hora = px.bar(
            por_hora, x="hora_publicacion", y="engagement_rate_pct",
            labels={"hora_publicacion": "Hora de publicación (UTC)", "engagement_rate_pct": "Engagement (%)"},
        )
        st.plotly_chart(fig_hora, width="stretch")

    # ---- Tabla de publicaciones ----
    st.subheader("Detalle de publicaciones")
    columnas_tabla = [
        "timestamp_publicacion", "media_type", "like_count", "comments_count",
        "reach", "views", "total_interactions", "engagement_rate_pct", "permalink",
    ]
    st.dataframe(
        df_filtrado[columnas_tabla].sort_values("timestamp_publicacion", ascending=False),
        width="stretch",
    )


if __name__ == "__main__":
    main()
    