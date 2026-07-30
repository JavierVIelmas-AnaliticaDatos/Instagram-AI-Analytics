"""
app.py

Dashboard de analítica de Instagram: visualiza engagement, alcance y
desempeño por tipo de contenido a partir de los datos guardados en Postgres (Neon).

Soporta múltiples cuentas de Instagram: las credenciales (nombre, access_token,
instagram_account_id) se guardan en la tabla 'cuentas_instagram' y se seleccionan
desde un dropdown en la barra lateral.

Requiere en .env / Streamlit secrets:
    NEON_DB_URL

Uso local:
    streamlit run app.py
"""

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from sqlalchemy import create_engine, text

from config import NEON_DB_URL

GRAPH_API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

METRICS_BY_TYPE = {
    "IMAGE": ["reach", "saved", "likes", "comments", "shares", "total_interactions"],
    "CAROUSEL_ALBUM": ["reach", "saved", "likes", "comments", "shares", "total_interactions"],
    "VIDEO": ["reach", "saved", "likes", "comments", "shares", "total_interactions", "views"],
    "REEL": ["reach", "saved", "likes", "comments", "shares", "total_interactions", "views"],
}

st.set_page_config(
    page_title="Instagram Analytics",
    page_icon="📸",
    layout="wide",
)


@st.cache_resource
def get_engine():
    return create_engine(NEON_DB_URL)


# ---------------------------------------------------------------------------
# Gestión de cuentas (multi-cuenta)
# ---------------------------------------------------------------------------

def crear_tablas_si_no_existen(engine):
    ddl_cuentas = """
    CREATE TABLE IF NOT EXISTS cuentas_instagram (
        id                    SERIAL PRIMARY KEY,
        nombre                TEXT UNIQUE NOT NULL,
        access_token          TEXT NOT NULL,
        instagram_account_id  TEXT NOT NULL,
        fecha_creacion        TIMESTAMPTZ DEFAULT now()
    );
    """
    ddl_publicaciones = """
    CREATE TABLE IF NOT EXISTS publicaciones_instagram (
        media_id              TEXT PRIMARY KEY,
        cuenta_id             TEXT,
        media_type            TEXT,
        caption               TEXT,
        permalink             TEXT,
        timestamp_publicacion TIMESTAMPTZ,
        like_count            INTEGER,
        comments_count        INTEGER,
        reach                 INTEGER,
        saved                 INTEGER,
        shares                INTEGER,
        total_interactions    INTEGER,
        views                 INTEGER,
        fecha_extraccion      TIMESTAMPTZ DEFAULT now()
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl_cuentas))
        conn.execute(text(ddl_publicaciones))


@st.cache_data(ttl=60)
def cargar_cuentas():
    engine = get_engine()
    query = "SELECT id, nombre, access_token, instagram_account_id FROM cuentas_instagram ORDER BY nombre;"
    return pd.read_sql(query, engine)


def guardar_cuenta(nombre, access_token, instagram_account_id):
    engine = get_engine()
    upsert_sql = """
    INSERT INTO cuentas_instagram (nombre, access_token, instagram_account_id)
    VALUES (:nombre, :access_token, :instagram_account_id)
    ON CONFLICT (nombre) DO UPDATE SET
        access_token = EXCLUDED.access_token,
        instagram_account_id = EXCLUDED.instagram_account_id;
    """
    with engine.begin() as conn:
        conn.execute(text(upsert_sql), {
            "nombre": nombre,
            "access_token": access_token,
            "instagram_account_id": instagram_account_id,
        })


def eliminar_cuenta(nombre):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cuentas_instagram WHERE nombre = :nombre"), {"nombre": nombre})


# ---------------------------------------------------------------------------
# Extracción desde Meta Graph API
# ---------------------------------------------------------------------------

def obtener_publicaciones(access_token, instagram_account_id):
    publicaciones = []
    url = (
        f"{BASE_URL}/{instagram_account_id}/media"
        f"?fields=id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count"
        f"&access_token={access_token}"
    )
    while url:
        response = requests.get(url)
        data = response.json()
        if "error" in data:
            raise RuntimeError(data["error"]["message"])
        publicaciones.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
    return publicaciones


def obtener_insights(media_id, media_type, media_product_type, access_token):
    tipo_clave = "REEL" if media_product_type == "REELS" else media_type
    metricas = METRICS_BY_TYPE.get(tipo_clave)
    if not metricas:
        return {}

    url = (
        f"{BASE_URL}/{media_id}/insights"
        f"?metric={','.join(metricas)}"
        f"&access_token={access_token}"
    )
    response = requests.get(url)
    data = response.json()
    if "error" in data:
        return {}

    resultado = {}
    for item in data.get("data", []):
        valores = item.get("values", [])
        if valores:
            resultado[item["name"]] = valores[0].get("value", 0)
    return resultado


def guardar_publicacion(engine, post, insights, instagram_account_id):
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
            "cuenta_id": instagram_account_id,
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


def refrescar_datos_instagram(access_token, instagram_account_id):
    """Descarga publicaciones nuevas de Instagram y actualiza Postgres.
    Devuelve (exito: bool, mensaje: str)."""
    if not access_token or not instagram_account_id:
        return False, "Falta el token o el ID de cuenta de Instagram."

    engine = get_engine()

    try:
        publicaciones = obtener_publicaciones(access_token, instagram_account_id)
    except RuntimeError as e:
        return False, f"Error de la API de Meta: {e}"

    if not publicaciones:
        return True, "No se encontraron publicaciones nuevas."

    for post in publicaciones:
        insights = obtener_insights(
            post["id"], post.get("media_type"), post.get("media_product_type"), access_token
        )
        guardar_publicacion(engine, post, insights, instagram_account_id)

    return True, f"{len(publicaciones)} publicaciones actualizadas correctamente."


# ---------------------------------------------------------------------------
# Datos y analítica
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def cargar_datos(instagram_account_id):
    engine = get_engine()
    query = "SELECT * FROM publicaciones_instagram WHERE cuenta_id = :cuenta_id ORDER BY timestamp_publicacion;"
    df = pd.read_sql(text(query), engine, params={"cuenta_id": instagram_account_id})
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


# ---------------------------------------------------------------------------
# Selector de cuenta en la barra lateral
# ---------------------------------------------------------------------------

def selector_de_cuenta():
    st.sidebar.header("Cuenta de Instagram")

    cuentas_df = cargar_cuentas()

    opciones = cuentas_df["nombre"].tolist() + ["➕ Agregar nueva cuenta..."]
    seleccion = st.sidebar.selectbox("Selecciona una cuenta", opciones)

    if seleccion == "➕ Agregar nueva cuenta...":
        with st.sidebar.form("form_nueva_cuenta"):
            st.write("**Nueva cuenta**")
            nombre = st.text_input("Nombre para identificarla (ej. 'Cliente A')")
            token = st.text_input("Access Token", type="password")
            account_id = st.text_input("Instagram Account ID")
            guardar = st.form_submit_button("💾 Guardar cuenta")

        if guardar:
            if not nombre or not token or not account_id:
                st.sidebar.error("Completa los tres campos.")
                return None
            guardar_cuenta(nombre, token, account_id)
            st.cache_data.clear()
            st.sidebar.success(f"Cuenta '{nombre}' guardada.")
            st.rerun()

        return None

    fila = cuentas_df[cuentas_df["nombre"] == seleccion].iloc[0]

    with st.sidebar.expander("⚙️ Gestionar esta cuenta"):
        st.caption(f"Instagram Account ID: {fila['instagram_account_id']}")
        if st.button("🗑️ Eliminar esta cuenta", width="stretch"):
            eliminar_cuenta(seleccion)
            st.cache_data.clear()
            st.rerun()

    return {
        "nombre": fila["nombre"],
        "access_token": fila["access_token"],
        "instagram_account_id": fila["instagram_account_id"],
    }


# ---------------------------------------------------------------------------
# App principal
# ---------------------------------------------------------------------------

def main():
    st.title("📸 Instagram Analytics Dashboard")
    st.caption("Análisis de interacción y desempeño de publicaciones")

    engine = get_engine()
    crear_tablas_si_no_existen(engine)

    cuenta = selector_de_cuenta()

    if cuenta is None:
        st.info("👈 Selecciona o agrega una cuenta de Instagram en la barra lateral para comenzar.")
        return

    st.sidebar.divider()

    if st.sidebar.button("🔄 Refrescar datos de Instagram", width="stretch"):
        with st.spinner(f"Descargando publicaciones de '{cuenta['nombre']}'..."):
            exito, mensaje = refrescar_datos_instagram(
                cuenta["access_token"], cuenta["instagram_account_id"]
            )
        if exito:
            st.sidebar.success(mensaje)
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(mensaje)

    st.sidebar.divider()

    df = cargar_datos(cuenta["instagram_account_id"])

    if df.empty:
        st.warning("⚠️ No hay publicaciones para esta cuenta todavía. Usa el botón de refrescar en la barra lateral.")
        return

    df = preparar_datos(df)

    # ---- Filtros adicionales ----
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

    