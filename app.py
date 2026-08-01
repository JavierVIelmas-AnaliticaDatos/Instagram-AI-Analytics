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

import re
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
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
        followers_count       INTEGER,
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
    ddl_historial_seguidores = """
    CREATE TABLE IF NOT EXISTS historial_seguidores (
        cuenta_id       TEXT NOT NULL,
        fecha           DATE NOT NULL,
        followers_count INTEGER,
        PRIMARY KEY (cuenta_id, fecha)
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl_cuentas))
        conn.execute(text(ddl_publicaciones))
        conn.execute(text(ddl_historial_seguidores))
        # Migración: agrega followers_count si la tabla ya existía de una versión anterior
        conn.execute(text("ALTER TABLE cuentas_instagram ADD COLUMN IF NOT EXISTS followers_count INTEGER;"))


@st.cache_data(ttl=60)
def cargar_cuentas():
    engine = get_engine()
    query = "SELECT id, nombre, access_token, instagram_account_id, followers_count FROM cuentas_instagram ORDER BY nombre;"
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

def obtener_followers_count(access_token, instagram_account_id):
    url = f"{BASE_URL}/{instagram_account_id}?fields=followers_count&access_token={access_token}"
    response = requests.get(url)
    data = response.json()
    if "error" in data:
        return None
    return data.get("followers_count")


def actualizar_followers_count(nombre, instagram_account_id, followers_count):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE cuentas_instagram SET followers_count = :fc WHERE nombre = :nombre"),
            {"fc": followers_count, "nombre": nombre},
        )
        # Guarda un snapshot histórico (uno por día; si ya existe uno hoy, lo actualiza)
        conn.execute(
            text("""
                INSERT INTO historial_seguidores (cuenta_id, fecha, followers_count)
                VALUES (:cuenta_id, CURRENT_DATE, :fc)
                ON CONFLICT (cuenta_id, fecha) DO UPDATE SET followers_count = EXCLUDED.followers_count;
            """),
            {"cuenta_id": instagram_account_id, "fc": followers_count},
        )


@st.cache_data(ttl=300)
def cargar_historial_seguidores(instagram_account_id):
    engine = get_engine()
    query = "SELECT fecha, followers_count FROM historial_seguidores WHERE cuenta_id = :cuenta_id ORDER BY fecha;"
    return pd.read_sql(text(query), engine, params={"cuenta_id": instagram_account_id})


def obtener_historial_seguidores_api(access_token, instagram_account_id, dias=30):
    """Trae la serie histórica de follower_count de los últimos N días desde Meta.
    Meta solo retiene esta métrica por un período limitado (~30 días), por eso
    conviene hacer este backfill lo antes posible al conectar una cuenta nueva."""
    until = datetime.now()
    since = until - timedelta(days=dias)

    url = (
        f"{BASE_URL}/{instagram_account_id}/insights"
        f"?metric=follower_count&period=day&metric_type=time_series"
        f"&since={int(since.timestamp())}&until={int(until.timestamp())}"
        f"&access_token={access_token}"
    )
    response = requests.get(url)
    data = response.json()

    if "error" in data:
        return None, data["error"]["message"]

    resultados = data.get("data", [])
    if not resultados:
        return [], None

    valores = resultados[0].get("values", [])
    serie = []
    for punto in valores:
        fecha = punto["end_time"][:10]  # "2026-07-01T07:00:00+0000" -> "2026-07-01"
        serie.append({"fecha": fecha, "followers_count": punto["value"]})

    return serie, None


def guardar_historial_seguidores_bulk(instagram_account_id, serie):
    """Inserta/actualiza varios puntos históricos de una sola vez.
    Usa ON CONFLICT DO UPDATE, así que NUNCA borra registros existentes —
    solo llena huecos o corrige el valor de un día si ya existía."""
    if not serie:
        return 0

    engine = get_engine()
    upsert_sql = """
        INSERT INTO historial_seguidores (cuenta_id, fecha, followers_count)
        VALUES (:cuenta_id, :fecha, :followers_count)
        ON CONFLICT (cuenta_id, fecha) DO UPDATE SET followers_count = EXCLUDED.followers_count;
    """
    with engine.begin() as conn:
        for punto in serie:
            conn.execute(text(upsert_sql), {
                "cuenta_id": instagram_account_id,
                "fecha": punto["fecha"],
                "followers_count": punto["followers_count"],
            })
    return len(serie)


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


def refrescar_datos_instagram(nombre_cuenta, access_token, instagram_account_id):
    """Descarga publicaciones nuevas de Instagram y actualiza Postgres.
    Devuelve (exito: bool, mensaje: str)."""
    if not access_token or not instagram_account_id:
        return False, "Falta el token o el ID de cuenta de Instagram."

    engine = get_engine()

    followers_count = obtener_followers_count(access_token, instagram_account_id)
    if followers_count is not None:
        actualizar_followers_count(nombre_cuenta, instagram_account_id, followers_count)

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


def preparar_datos(df, followers_count=None):
    df["timestamp_publicacion"] = pd.to_datetime(df["timestamp_publicacion"])
    df["fecha"] = df["timestamp_publicacion"].dt.date
    df["dia_semana"] = df["timestamp_publicacion"].dt.day_name()
    df["hora_publicacion"] = df["timestamp_publicacion"].dt.hour
    df["mes_anio"] = df["timestamp_publicacion"].dt.to_period("M").astype(str)

    columnas_numericas = [
        "like_count", "comments_count", "reach",
        "saved", "shares", "total_interactions", "views",
    ]
    for col in columnas_numericas:
        df[col] = df[col].fillna(0)

    # Engagement rate basado en reach (como antes) — sensible al alcance puntual del post
    df["engagement_rate_pct"] = df.apply(
        lambda row: round((row["total_interactions"] / row["reach"]) * 100, 2)
        if row["reach"] > 0 else 0,
        axis=1,
    )

    # Engagement rate basado en seguidores — métrica estándar de la industria,
    # más estable porque no depende de las fluctuaciones del algoritmo en el reach.
    if followers_count and followers_count > 0:
        df["engagement_rate_followers_pct"] = round(
            (df["total_interactions"] / followers_count) * 100, 2
        )
    else:
        df["engagement_rate_followers_pct"] = None

    # --- Análisis de contenido: caption y hashtags ---
    df["caption"] = df["caption"].fillna("")
    df["caption_length"] = df["caption"].str.len()
    df["hashtags"] = df["caption"].apply(lambda c: re.findall(r"#(\w+)", c))
    df["num_hashtags"] = df["hashtags"].apply(len)

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

            with st.spinner("Cargando historial de seguidores de los últimos 30 días..."):
                serie, error = obtener_historial_seguidores_api(token, account_id)
            if error:
                st.sidebar.warning(f"Cuenta guardada, pero no se pudo cargar el historial: {error}")
            else:
                n = guardar_historial_seguidores_bulk(account_id, serie)
                st.sidebar.success(f"Cuenta '{nombre}' guardada, con {n} días de historial cargados.")

            st.cache_data.clear()
            st.rerun()

        return None

    fila = cuentas_df[cuentas_df["nombre"] == seleccion].iloc[0]

    with st.sidebar.expander("⚙️ Gestionar esta cuenta"):
        st.caption(f"Instagram Account ID: {fila['instagram_account_id']}")

        if st.button("📥 Cargar historial de seguidores (30 días)", width="stretch"):
            with st.spinner("Consultando historial en Meta..."):
                serie, error = obtener_historial_seguidores_api(
                    fila["access_token"], fila["instagram_account_id"]
                )
            if error:
                st.error(f"No se pudo cargar el historial: {error}")
            else:
                n = guardar_historial_seguidores_bulk(fila["instagram_account_id"], serie)
                st.success(f"{n} días de historial cargados/actualizados.")
                st.cache_data.clear()
                st.rerun()

        if st.button("🗑️ Eliminar esta cuenta", width="stretch"):
            eliminar_cuenta(seleccion)
            st.cache_data.clear()
            st.rerun()

    return {
        "nombre": fila["nombre"],
        "access_token": fila["access_token"],
        "instagram_account_id": fila["instagram_account_id"],
        "followers_count": fila["followers_count"],
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
                cuenta["nombre"], cuenta["access_token"], cuenta["instagram_account_id"]
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

    df = preparar_datos(df, followers_count=cuenta["followers_count"])

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
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Publicaciones", len(df_filtrado))
    col2.metric("Seguidores", f"{cuenta['followers_count']:,}" if cuenta["followers_count"] else "—")
    col3.metric("Likes promedio", f"{df_filtrado['like_count'].mean():.1f}")
    col4.metric("Reach promedio", f"{df_filtrado['reach'].mean():.1f}")
    col5.metric("Engagement / reach", f"{df_filtrado['engagement_rate_pct'].mean():.1f}%")
    if df_filtrado["engagement_rate_followers_pct"].notna().any():
        col6.metric("Engagement / seguidores", f"{df_filtrado['engagement_rate_followers_pct'].mean():.2f}%")
    else:
        col6.metric("Engagement / seguidores", "—")

    st.caption(
        "💡 **Engagement / reach**: interacciones sobre el alcance de cada post (sensible a "
        "fluctuaciones del algoritmo). **Engagement / seguidores**: interacciones sobre el total "
        "de seguidores actuales — métrica estándar de la industria, más estable para comparar "
        "publicaciones entre sí."
    )

    st.divider()

    # ---- Crecimiento de seguidores en el tiempo ----
    st.subheader("📈 Crecimiento de la cuenta")
    historial = cargar_historial_seguidores(cuenta["instagram_account_id"])

    if len(historial) < 2:
        st.info(
            "Aún no hay suficiente historial para graficar el crecimiento. "
            "Cada vez que uses '🔄 Refrescar datos' se guarda un registro nuevo — "
            "con el tiempo este gráfico mostrará la evolución real de seguidores."
        )
    else:
        fig_seguidores = px.line(
            historial, x="fecha", y="followers_count", markers=True,
            labels={"fecha": "Fecha", "followers_count": "Seguidores"},
        )
        st.plotly_chart(fig_seguidores, width="stretch")

    # ---- Publicaciones vs. cambio de seguidores por día (correlación, no causalidad) ----
    if len(historial) >= 2:
        st.subheader("📊 Publicaciones vs. cambio de seguidores")
        st.caption(
            "⚠️ Meta no permite atribuir el crecimiento de seguidores a una publicación "
            "específica. Este gráfico muestra ambas series en el mismo día para detectar "
            "**correlaciones visuales** (no causalidad comprobada) — útil como pista, no como prueba."
        )

        historial_calc = historial.copy()
        historial_calc["fecha"] = pd.to_datetime(historial_calc["fecha"]).dt.date
        historial_calc["cambio_neto"] = historial_calc["followers_count"].diff()

        posts_por_dia = df_filtrado.groupby("fecha", as_index=False).size().rename(columns={"size": "publicaciones"})

        fig_combinado = make_subplots(specs=[[{"secondary_y": True}]])
        fig_combinado.add_trace(
            go.Bar(x=posts_por_dia["fecha"], y=posts_por_dia["publicaciones"], name="Publicaciones"),
            secondary_y=False,
        )
        fig_combinado.add_trace(
            go.Scatter(
                x=historial_calc["fecha"], y=historial_calc["cambio_neto"],
                name="Cambio neto de seguidores", mode="lines+markers",
            ),
            secondary_y=True,
        )
        fig_combinado.update_yaxes(title_text="N° publicaciones", secondary_y=False)
        fig_combinado.update_yaxes(title_text="Cambio neto de seguidores", secondary_y=True)
        st.plotly_chart(fig_combinado, width="stretch")

    st.divider()

    # ---- Frecuencia de publicación ----
    fechas_ordenadas = df_filtrado["timestamp_publicacion"].sort_values()
    if len(fechas_ordenadas) >= 2:
        dias_entre_posts = fechas_ordenadas.diff().dt.total_seconds().dropna() / 86400
        promedio_dias = dias_entre_posts.mean()
        col_freq1, col_freq2 = st.columns(2)
        col_freq1.metric("Días promedio entre publicaciones", f"{promedio_dias:.1f}")
        col_freq2.metric("Publicaciones por semana (aprox.)", f"{7 / promedio_dias:.1f}" if promedio_dias > 0 else "—")

    st.divider()

    # ---- Comparación mensual ----
    st.subheader("📅 Comparación mes a mes")
    meses_disponibles = df_filtrado["mes_anio"].nunique()
    if meses_disponibles < 2:
        st.info(
            f"Todas las publicaciones filtradas caen en el mismo mes ({df_filtrado['mes_anio'].iloc[0]}). "
            "Cuando tengas publicaciones de varios meses, aquí verás la comparación de desempeño entre ellos."
        )
    else:
        resumen_mensual = df_filtrado.groupby("mes_anio", as_index=False).agg(
            publicaciones=("media_id", "count"),
            likes_promedio=("like_count", "mean"),
            reach_promedio=("reach", "mean"),
            engagement_promedio=("engagement_rate_pct", "mean"),
        ).round(2)

        col_mes1, col_mes2 = st.columns(2)
        with col_mes1:
            fig_mes_posts = px.bar(
                resumen_mensual, x="mes_anio", y="publicaciones",
                labels={"mes_anio": "Mes", "publicaciones": "N° publicaciones"},
                title="Publicaciones por mes",
            )
            st.plotly_chart(fig_mes_posts, width="stretch")
        with col_mes2:
            fig_mes_eng = px.bar(
                resumen_mensual, x="mes_anio", y="engagement_promedio",
                labels={"mes_anio": "Mes", "engagement_promedio": "Engagement (%)"},
                title="Engagement promedio por mes",
            )
            st.plotly_chart(fig_mes_eng, width="stretch")

        st.dataframe(resumen_mensual, width="stretch")

    st.divider()

    # ---- Mejor y peor racha (rolling window de 3 publicaciones) ----
    st.subheader("🔥 Mejor y peor racha")
    VENTANA = 3
    if len(df_filtrado) < VENTANA:
        st.info(f"Se necesitan al menos {VENTANA} publicaciones para detectar rachas. Tienes {len(df_filtrado)}.")
    else:
        df_ordenado = df_filtrado.sort_values("timestamp_publicacion").reset_index(drop=True)
        df_ordenado["racha_engagement"] = df_ordenado["engagement_rate_pct"].rolling(VENTANA).mean()

        idx_mejor = df_ordenado["racha_engagement"].idxmax()
        idx_peor = df_ordenado["racha_engagement"].idxmin()

        col_mejor, col_peor = st.columns(2)
        with col_mejor:
            st.markdown(f"**🏆 Mejor racha** ({VENTANA} publicaciones consecutivas)")
            racha_mejor = df_ordenado.iloc[idx_mejor - VENTANA + 1: idx_mejor + 1]
            st.metric("Engagement promedio de la racha", f"{df_ordenado.loc[idx_mejor, 'racha_engagement']:.2f}%")
            st.dataframe(
                racha_mejor[["timestamp_publicacion", "media_type", "engagement_rate_pct", "permalink"]],
                width="stretch",
            )
        with col_peor:
            st.markdown(f"**📉 Peor racha** ({VENTANA} publicaciones consecutivas)")
            racha_peor = df_ordenado.iloc[idx_peor - VENTANA + 1: idx_peor + 1]
            st.metric("Engagement promedio de la racha", f"{df_ordenado.loc[idx_peor, 'racha_engagement']:.2f}%")
            st.dataframe(
                racha_peor[["timestamp_publicacion", "media_type", "engagement_rate_pct", "permalink"]],
                width="stretch",
            )

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

    # ---- Análisis de contenido: hashtags y longitud de caption ----
    st.subheader("Análisis de contenido")
    col_hash, col_caption = st.columns(2)

    with col_hash:
        st.markdown("**Hashtags más usados y su engagement promedio**")
        hashtags_expandidos = df_filtrado.explode("hashtags").dropna(subset=["hashtags"])
        if hashtags_expandidos.empty:
            st.info("No se detectaron hashtags en las publicaciones filtradas.")
        else:
            resumen_hashtags = (
                hashtags_expandidos.groupby("hashtags")
                .agg(usos=("media_id", "count"), engagement_promedio=("engagement_rate_pct", "mean"))
                .sort_values("usos", ascending=False)
                .head(10)
                .round(2)
            )
            st.dataframe(resumen_hashtags, width="stretch")

    with col_caption:
        st.markdown("**Longitud del caption vs. engagement**")
        if df_filtrado["caption_length"].sum() == 0:
            st.info("Las publicaciones filtradas no tienen texto en el caption.")
        else:
            fig_caption = px.scatter(
                df_filtrado,
                x="caption_length",
                y="engagement_rate_pct",
                color="media_type",
                size="num_hashtags",
                hover_data=["permalink"],
                labels={
                    "caption_length": "Longitud del caption (caracteres)",
                    "engagement_rate_pct": "Engagement (%)",
                    "num_hashtags": "N° hashtags",
                },
            )
            st.plotly_chart(fig_caption, width="stretch")

    st.divider()

    # ---- Tabla de publicaciones ----
    st.subheader("Detalle de publicaciones")
    columnas_tabla = [
        "timestamp_publicacion", "media_type", "like_count", "comments_count",
        "reach", "views", "total_interactions", "engagement_rate_pct",
        "num_hashtags", "caption_length", "permalink",
    ]
    st.dataframe(
        df_filtrado[columnas_tabla].sort_values("timestamp_publicacion", ascending=False),
        width="stretch",
    )


if __name__ == "__main__":
    main()