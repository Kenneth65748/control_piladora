from datetime import date, datetime
import os
import sqlite3
import libsql
import pandas as pd
import streamlit as st

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "piladora_arroz.db")
META_ACEITE = 30  # Quintales / sacos para cambio de aceite


def _usa_turso() -> bool:
    try:
        url = st.secrets.get("TURSO_DATABASE_URL", "")
        token = st.secrets.get("TURSO_AUTH_TOKEN", "")
        return bool(str(url).strip() and str(token).strip())
    except Exception:
        return False


class _CursorWrapper:
    def __init__(self, cur, columns=None):
        self._cur = cur
        self._columns = columns

    def _row_to_dict(self, row):
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        if hasattr(row, "keys"):
            return {k: row[k] for k in row.keys()}
        if self._columns:
            return {col: row[i] for i, col in enumerate(self._columns)}
        if isinstance(row, (tuple, list)):
            return {f"col_{i}": val for i, val in enumerate(row)}
        return row

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if isinstance(row, tuple) and not self._columns:
            return row
        return self._row_to_dict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], tuple) and not self._columns:
            return rows
        return [self._row_to_dict(r) for r in rows]

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)


class _ConnWrapper:
    def __init__(self, conn, is_turso=False):
        self._conn = conn
        self.is_turso = is_turso

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            if hasattr(self._conn, "commit"):
                self._conn.commit()
        else:
            if hasattr(self._conn, "rollback"):
                self._conn.rollback()
        self.close()
        return False

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        cols = None
        if getattr(cur, "description", None):
            cols = [d[0] for d in cur.description]
        return _CursorWrapper(cur, columns=cols)

    def commit(self):
        if hasattr(self._conn, "commit"):
            self._conn.commit()

    def rollback(self):
        if hasattr(self._conn, "rollback"):
            self._conn.rollback()

    def close(self):
        if hasattr(self._conn, "close"):
            self._conn.close()


def get_conn():
    try:
        turso_url = str(st.secrets["TURSO_DATABASE_URL"]).strip()
        turso_token = str(st.secrets["TURSO_AUTH_TOKEN"]).strip()
        if turso_url and turso_token:
            raw = libsql.connect(database=turso_url, auth_token=turso_token)
            return _ConnWrapper(raw, is_turso=True)
    except Exception:
        pass

    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return _ConnWrapper(conn, is_turso=False)


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS piladas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cliente TEXT NOT NULL,
                quintales REAL NOT NULL,
                monto REAL NOT NULL,
                saldo_pendiente REAL NOT NULL DEFAULT 0,
                estado TEXT NOT NULL DEFAULT 'Pendiente',
                fecha_registro TEXT NOT NULL,
                fecha_pago TEXT,
                nota TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS abonos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pilada_id INTEGER NOT NULL,
                cliente TEXT NOT NULL,
                monto_abono REAL NOT NULL,
                fecha_abono TEXT NOT NULL,
                nota TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS maquina (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                quintales_acumulados REAL NOT NULL DEFAULT 0,
                ultimo_cambio TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cambios_aceite (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_cambio TEXT NOT NULL,
                quintales_al_momento REAL NOT NULL,
                observacion TEXT
            )
        """)

        # Compatibilidad por si la tabla ya existía de la versión anterior sin saldo_pendiente
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(piladas)").fetchall()]
        if "saldo_pendiente" not in cols:
            conn.execute(
                "ALTER TABLE piladas ADD COLUMN saldo_pendiente REAL NOT NULL DEFAULT 0"
            )
            conn.execute("""
                UPDATE piladas
                SET saldo_pendiente = CASE WHEN estado = 'Cancelado' THEN 0 ELSE monto END
            """)

        existe_row = conn.execute("SELECT COUNT(*) AS cnt FROM maquina WHERE id = 1").fetchone()
        existe = existe_row["cnt"] if isinstance(existe_row, dict) else existe_row[0]
        if existe == 0:
            ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn.execute(
                "INSERT INTO maquina (id, quintales_acumulados, ultimo_cambio) VALUES (1, 0, ?)",
                (ahora,),
            )
        conn.commit()


def registrar_pilada(
    cliente: str, quintales: float, monto: float, estado: str, nota: str
):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    saldo = 0.0 if estado == "Cancelado" else float(monto)
    fecha_pago = ahora if estado == "Cancelado" else None
    cliente_limpio = cliente.strip()

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO piladas (cliente, quintales, monto, saldo_pendiente, estado, fecha_registro, fecha_pago, nota)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cliente_limpio,
                quintales,
                monto,
                saldo,
                estado,
                ahora,
                fecha_pago,
                nota.strip(),
            ),
        )
        pilada_id = cur.lastrowid
        if pilada_id is None:
            row_id = conn.execute(
                "SELECT id FROM piladas WHERE cliente = ? AND fecha_registro = ? ORDER BY id DESC LIMIT 1",
                (cliente_limpio, ahora),
            ).fetchone()
            pilada_id = row_id["id"] if isinstance(row_id, dict) else row_id[0]

        if estado == "Cancelado" and monto > 0:
            conn.execute(
                """
                INSERT INTO abonos (pilada_id, cliente, monto_abono, fecha_abono, nota)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pilada_id, cliente_limpio, monto, ahora, "Pago completo al registrar"),
            )

        conn.execute(
            """
            UPDATE maquina
            SET quintales_acumulados = quintales_acumulados + ?
            WHERE id = 1
            """,
            (quintales,),
        )
        conn.commit()


def registrar_abono(pilada_id: int, monto_abono: float, nota: str = ""):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM piladas WHERE id = ?", (pilada_id,)
        ).fetchone()
        if not row:
            return
        saldo_actual = float(
            row["saldo_pendiente"]
            if row["saldo_pendiente"] is not None
            else row["monto"]
        )
        abono_aplicado = min(float(monto_abono), saldo_actual)
        nuevo_saldo = max(0.0, saldo_actual - abono_aplicado)
        nuevo_estado = "Cancelado" if nuevo_saldo <= 0.001 else "Pendiente"
        fecha_pago = ahora if nuevo_estado == "Cancelado" else row["fecha_pago"]

        conn.execute(
            """
            UPDATE piladas
            SET saldo_pendiente = ?, estado = ?, fecha_pago = ?
            WHERE id = ?
            """,
            (round(nuevo_saldo, 2), nuevo_estado, fecha_pago, pilada_id),
        )
        conn.execute(
            """
            INSERT INTO abonos (pilada_id, cliente, monto_abono, fecha_abono, nota)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                pilada_id,
                row["cliente"],
                round(abono_aplicado, 2),
                ahora,
                nota.strip() or "Abono a cuenta",
            ),
        )
        conn.commit()


def marcar_cancelado(pilada_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT saldo_pendiente, monto FROM piladas WHERE id = ?", (pilada_id,)
        ).fetchone()
        if not row:
            return
        pendiente = float(
            row["saldo_pendiente"]
            if row["saldo_pendiente"] is not None
            else row["monto"]
        )
    if pendiente > 0:
        registrar_abono(pilada_id, pendiente, "Cancelación total")
    else:
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
        with get_conn() as conn:
            conn.execute(
                "UPDATE piladas SET estado = 'Cancelado', saldo_pendiente = 0, fecha_pago = ? WHERE id = ?",
                (ahora, pilada_id),
            )
            conn.commit()


def editar_pilada(
    pilada_id: int,
    cliente: str,
    quintales: float,
    monto: float,
    saldo_pendiente: float,
    estado: str,
    nota: str,
):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    saldo_final = 0.0 if estado == "Cancelado" else float(saldo_pendiente)
    fecha_pago = ahora if estado == "Cancelado" else None
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE piladas
            SET cliente = ?, quintales = ?, monto = ?, saldo_pendiente = ?, estado = ?, fecha_pago = ?, nota = ?
            WHERE id = ?
            """,
            (
                cliente.strip(),
                quintales,
                monto,
                saldo_final,
                estado,
                fecha_pago,
                nota.strip(),
                pilada_id,
            ),
        )
        conn.commit()


def eliminar_pilada(pilada_id: int, ajustar_maquina: bool = True):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT quintales FROM piladas WHERE id = ?", (pilada_id,)
        ).fetchone()
        if row and ajustar_maquina:
            actual_row = conn.execute(
                "SELECT quintales_acumulados FROM maquina WHERE id = 1"
            ).fetchone()
            actual_val = float(actual_row["quintales_acumulados"] or 0.0)
            nuevo_val = max(0.0, actual_val - float(row["quintales"]))
            conn.execute(
                """
                UPDATE maquina
                SET quintales_acumulados = ?
                WHERE id = 1
                """,
                (nuevo_val,),
            )
        conn.execute("DELETE FROM abonos WHERE pilada_id = ?", (pilada_id,))
        conn.execute("DELETE FROM piladas WHERE id = ?", (pilada_id,))
        conn.commit()


def registrar_cambio_aceite(observacion: str):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    with get_conn() as conn:
        actual = conn.execute(
            "SELECT quintales_acumulados FROM maquina WHERE id = 1"
        ).fetchone()["quintales_acumulados"]
        conn.execute(
            """
            INSERT INTO cambios_aceite (fecha_cambio, quintales_al_momento, observacion)
            VALUES (?, ?, ?)
            """,
            (ahora, actual, observacion.strip()),
        )
        conn.execute(
            """
            UPDATE maquina
            SET quintales_acumulados = 0, ultimo_cambio = ?
            WHERE id = 1
            """,
            (ahora,),
        )
        conn.commit()


def obtener_maquina():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT quintales_acumulados, ultimo_cambio FROM maquina WHERE id = 1"
        ).fetchone()
    return dict(row)


def obtener_piladas() -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM piladas ORDER BY id DESC").fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "id",
            "cliente",
            "quintales",
            "monto",
            "saldo_pendiente",
            "estado",
            "fecha_registro",
            "fecha_pago",
            "nota",
        ])
    df = pd.DataFrame([dict(r) for r in rows])
    df["saldo_pendiente"] = df.apply(
        lambda r: (
            0.0
            if r["estado"] == "Cancelado"
            else float(
                r["saldo_pendiente"] if pd.notna(r["saldo_pendiente"]) else r["monto"]
            )
        ),
        axis=1,
    )
    return df


def obtener_abonos() -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM abonos ORDER BY id DESC").fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["id", "pilada_id", "cliente", "monto_abono", "fecha_abono", "nota"]
        )
    return pd.DataFrame([dict(r) for r in rows])


def obtener_cambios_aceite() -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cambios_aceite ORDER BY id DESC"
        ).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["id", "fecha_cambio", "quintales_al_momento", "observacion"]
        )
    return pd.DataFrame([dict(r) for r in rows])


st.set_page_config(
    page_title="Control Piladora de Arroz", page_icon="🌾", layout="wide"
)
init_db()

# --- Fondo sutil estilo grano de arroz (sin afectar visibilidad ni números) ---
st.markdown(
    """
    <style>
    /* Patrón suave tipo granos de arroz en SVG codificado */
    .stApp {
        background-color: #0f172a;
        background-image:
            radial-gradient(circle at 15% 15%, rgba(234, 179, 8, 0.06), transparent 40%),
            radial-gradient(circle at 85% 20%, rgba(34, 197, 94, 0.05), transparent 45%),
            url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='90' height='90' viewBox='0 0 90 90'%3E%3Cg fill='none' stroke='%23f8fafc' stroke-opacity='0.035' stroke-width='1.1'%3E%3Cellipse cx='22' cy='24' rx='9' ry='3.4' transform='rotate(-28 22 24)'/%3E%3Cellipse cx='66' cy='18' rx='9' ry='3.4' transform='rotate(24 66 18)'/%3E%3Cellipse cx='46' cy='52' rx='9.5' ry='3.5' transform='rotate(-12 46 52)'/%3E%3Cellipse cx='18' cy='72' rx='9' ry='3.4' transform='rotate(32 18 72)'/%3E%3Cellipse cx='72' cy='68' rx='9' ry='3.4' transform='rotate(-35 72 68)'/%3E%3C/g%3E%3C/svg%3E");
        background-attachment: fixed;
    }

    /* Contenedores y tarjetas adaptables de alto contraste para lectura limpia */
    [data-testid="stMetric"] {
        background-color: rgba(15, 23, 42, 0.86);
        border: 1px solid rgba(148, 163, 184, 0.22);
        border-radius: 12px;
        padding: 14px 16px;
        backdrop-filter: blur(4px);
    }

    [data-testid="stVerticalBlockBorderWrapper"] {
        background-color: rgba(15, 23, 42, 0.84) !important;
        border-color: rgba(148, 163, 184, 0.25) !important;
        border-radius: 12px !important;
    }

    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🌾 Control de Piladora de Arroz")
st.caption(
    "Control de deudas, abonos, sacos/quintales pilados y alerta de cambio de"
    " aceite cada 30."
)

maq = obtener_maquina()
acumulados = float(maq["quintales_acumulados"] or 0.0)
df = obtener_piladas()
df_abonos = obtener_abonos()
df_aceite = obtener_cambios_aceite()

# --- Alerta de cambio de aceite ---
if acumulados >= META_ACEITE:
    extras = acumulados - META_ACEITE
    extra_txt = (
        f" (lleva {extras:g} sacos extra sobre el límite)" if extras > 0 else ""
    )
    st.error(
        f"🚨 CAMBIO DE ACEITE REQUERIDO: La máquina lleva {acumulados:g} de"
        f" {META_ACEITE} quintales/sacos{extra_txt}. ¡Ya toca cambiarle el"
        " aceite!"
    )
elif acumulados >= META_ACEITE - 5:
    st.warning(
        f"⚠️ Cerca del cambio de aceite: Llevas {acumulados:g} de"
        f" {META_ACEITE} quintales/sacos (faltan {META_ACEITE - acumulados:g})."
    )
else:
    st.success(
        f"✅ Aceite al día: {acumulados:g} de {META_ACEITE} quintales/sacos"
        f" acumulados (último cambio: {maq['ultimo_cambio']})."
    )

st.progress(
    min(acumulados / META_ACEITE, 1.0),
    text=f"Contador de aceite: {acumulados:g} / {META_ACEITE} sacos",
)

# --- Métricas generales y resumen del día ---
pendientes_df = df[df["estado"] == "Pendiente"] if not df.empty else pd.DataFrame()
deuda_total = (
    float(pendientes_df["saldo_pendiente"].sum())
    if not pendientes_df.empty
    else 0.0
)
abonado_total = (
    float(df_abonos["monto_abono"].sum()) if not df_abonos.empty else 0.0
)
total_pilado = float(df["quintales"].sum()) if not df.empty else 0.0

hoy_str = date.today().strftime("%Y-%m-%d")
df_hoy = (
    df[df["fecha_registro"].str.startswith(hoy_str)]
    if not df.empty
    else pd.DataFrame()
)
sacos_hoy = float(df_hoy["quintales"].sum()) if not df_hoy.empty else 0.0
deuda_hoy = (
    float(df_hoy[df_hoy["estado"] == "Pendiente"]["saldo_pendiente"].sum())
    if not df_hoy.empty
    else 0.0
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Sacos para Aceite", f"{acumulados:g} / {META_ACEITE}")
m2.metric("Personas Debiendo", len(pendientes_df))
m3.metric("Saldo Pendiente Total", f"₡{deuda_total:,.2f}")
m4.metric("Total Abonado / Cobrado", f"₡{abonado_total:,.2f}")

with st.expander("📅 Resumen de hoy", expanded=False):
    h1, h2, h3 = st.columns(3)
    h1.metric("Sacos Pilados Hoy", f"{sacos_hoy:g}")
    h2.metric("Pendiente Generado Hoy", f"₡{deuda_hoy:,.2f}")
    h3.metric("Total Quintales Históricos", f"{total_pilado:g}")

st.divider()

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "➕ Registrar Pilada",
    f"💸 Quienes Deben ({len(pendientes_df)})",
    "👤 Por Cliente",
    "📋 Historial y Edición",
    f"🛢️ Cambio de Aceite ({len(df_aceite)})",
])

# --- TAB 1: REGISTRAR PILADA ---
with tab1:
    st.subheader("Registrar pilada")
    with st.form("form_pilada", clear_on_submit=True):
        c1, c2 = st.columns(2)
        cliente = c1.text_input("Nombre de la persona *")
        unidad = c2.radio(
            "Medida *",
            ["Quintales", "Latas (3 = 1 qq)"],
            horizontal=True,
        )
        if unidad == "Quintales":
            quintales = c2.number_input(
                "Cantidad de sacos / quintales *",
                min_value=0.0,
                max_value=500.0,
                value=1.0,
                step=0.5,
            )
        else:
            latas = c2.number_input(
                "Cantidad de latas *",
                min_value=0.0,
                max_value=1500.0,
                value=3.0,
                step=0.5,
            )
            quintales = round(latas / 3, 4)
            c2.caption(f"Equivale a: {quintales:.2f} qq")

        c3, c4 = st.columns(2)
        monto = c3.number_input(
            "Monto (₡) *", min_value=0.0, value=0.0, step=500.0
        )
        estado = c4.selectbox("Estado *", ["Pendiente", "Cancelado"])

        nota = st.text_input(
            "Nota (opcional)", placeholder="Ej: pago parcial, arroz húmedo, etc."
        )
        guardar = st.form_submit_button(
            "Guardar y sumar a la máquina", type="primary", use_container_width=True
        )

        if guardar:
            if not cliente.strip():
                st.error("Escribe el nombre de la persona.")
            else:
                registrar_pilada(cliente, quintales, monto, estado, nota)
                st.success(
                    f"Guardado: {cliente.strip()} — {quintales:g} sacos ({estado})."
                )
                st.rerun()

# --- TAB 2: QUIENES DEBEN (ABONOS PARCIALES Y PAGO TOTAL) ---
with tab2:
    st.subheader("Cuentas por cobrar")
    if pendientes_df.empty:
        st.info("Nadie queda debiendo por ahora.")
    else:
        filtro_deudor = st.text_input(
            "Buscar deudor por nombre", placeholder="Escriba un nombre..."
        )
        lista_deudores = pendientes_df.copy()
        if filtro_deudor.strip():
            q = filtro_deudor.strip().lower()
            lista_deudores = lista_deudores[
                lista_deudores["cliente"].str.lower().str.contains(q, na=False)
            ]

        for _, row in lista_deudores.iterrows():
            saldo_row = float(row["saldo_pendiente"])
            monto_row = float(row["monto"])
            with st.container(border=True):
                col_a, col_b, col_c, col_d = st.columns([3, 1.5, 2, 2.5])
                col_a.markdown(f"{row['cliente']}")
                col_a.caption(
                    f"#{row['id']} • {row['fecha_registro']}"
                    + (f" • {row['nota']}" if row["nota"] else "")
                )
                col_b.markdown(f"{row['quintales']:g} sacos")
                col_c.markdown(f"Debe: ₡{saldo_row:,.2f}")
                if monto_row > saldo_row:
                    col_c.caption(f"Monto original: ₡{monto_row:,.2f}")

                with col_d:
                    if col_d.button(
                        "✅ Pagó todo (Cancelar)",
                        key=f"pagar_{row['id']}",
                        type="primary",
                        use_container_width=True,
                    ):
                        marcar_cancelado(int(row["id"]))
                        st.rerun()

                    with st.popover("💵 Abonar parcial"):
                        abono_val = st.number_input(
                            "Monto del abono (₡)",
                            min_value=1.0,
                            max_value=max(1.0, saldo_row),
                            value=min(1000.0, max(1.0, saldo_row)),
                            step=500.0,
                            key=f"abono_num_{row['id']}",
                        )
                        nota_abono = st.text_input(
                            "Nota del abono", key=f"abono_nota_{row['id']}", placeholder="Opcional"
                        )
                        if st.button(
                            "Registrar abono",
                            key=f"abono_btn_{row['id']}",
                            use_container_width=True,
                        ):
                            registrar_abono(int(row["id"]), float(abono_val), nota_abono)
                            st.rerun()

# --- TAB 3: RESUMEN POR CLIENTE ---
with tab3:
    st.subheader("Estado de cuenta por cliente")
    if df.empty:
        st.info("Todavía no hay clientes registrados.")
    else:
        resumen_cli = df.groupby("cliente", as_index=False).agg(
            piladas=("id", "count"),
            sacos_totales=("quintales", "sum"),
            monto_facturado=("monto", "sum"),
            saldo_pendiente=("saldo_pendiente", "sum"),
        )
        abonos_cli = (
            df_abonos.groupby("cliente", as_index=False)["monto_abono"].sum()
            if not df_abonos.empty
            else pd.DataFrame(columns=["cliente", "monto_abono"])
        )
        resumen_cli = resumen_cli.merge(abonos_cli, on="cliente", how="left")
        resumen_cli["monto_abono"] = resumen_cli["monto_abono"].fillna(0.0)
        resumen_cli = resumen_cli.sort_values(
            by="saldo_pendiente", ascending=False
        )
        st.dataframe(resumen_cli, use_container_width=True, hide_index=True)

# --- TAB 4: HISTORIAL, FILTROS, EXPORTAR Y EDICIÓN ---
with tab4:
    st.subheader("Todos los registros")
    if df.empty:
        st.info("Aún no hay registros.")
    else:
        f1, f2, f3 = st.columns(3)
        estado_filtro = f1.selectbox("Estado", ["Todos", "Pendiente", "Cancelado"])
        buscar_txt = f2.text_input(
            "Buscar cliente o nota", placeholder="Nombre o detalle..."
        )
        solo_hoy = f3.checkbox("Solo registros de hoy")

        vista = df.copy()
        if estado_filtro != "Todos":
            vista = vista[vista["estado"] == estado_filtro]
        if buscar_txt.strip():
            q = buscar_txt.strip().lower()
            vista = vista[
                vista["cliente"].str.lower().str.contains(q, na=False)
                | vista["nota"].fillna("").str.lower().str.contains(q, na=False)
            ]
        if solo_hoy:
            vista = vista[vista["fecha_registro"].str.startswith(hoy_str)]

        st.dataframe(vista, use_container_width=True, hide_index=True)

        csv_bytes = vista.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Descargar historial filtrado (CSV)",
            data=csv_bytes,
            file_name=f"piladora_registros_{hoy_str}.csv",
            mime="text/csv",
        )

        if not df_abonos.empty:
            with st.expander("🧾 Ver historial de abonos y pagos"):
                st.dataframe(df_abonos, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Editar o eliminar un registro")
        opciones = {
            (
                f"#{int(r['id'])} — {r['cliente']} ({r['quintales']:g} sacos, debe"
                f" ₡{float(r['saldo_pendiente']):,.2f}, {r['estado']})"
            ): int(r["id"])
            for _, r in df.iterrows()
        }
        sel_id = opciones[
            st.selectbox("Selecciona un registro", list(opciones.keys()))
        ]
        fila = df[df["id"] == sel_id].iloc[0]

        with st.form("form_editar_pilada"):
            e1, e2, e3 = st.columns(3)
            e_cliente = e1.text_input("Cliente", value=str(fila["cliente"]))
            e_quintales = e2.number_input(
                "Sacos / quintales",
                min_value=0.5,
                max_value=500.0,
                value=float(fila["quintales"]),
                step=0.5,
            )
            e_monto = e3.number_input(
                "Monto total (₡)", min_value=0.0, value=float(fila["monto"]), step=500.0
            )

            e4, e5 = st.columns(2)
            e_saldo = e4.number_input(
                "Saldo pendiente (₡)",
                min_value=0.0,
                value=float(fila["saldo_pendiente"]),
                step=500.0,
            )
            e_estado = e5.selectbox(
                "Estado",
                ["Pendiente", "Cancelado"],
                index=0 if fila["estado"] == "Pendiente" else 1,
            )

            e_nota = st.text_input("Nota", value=str(fila["nota"] or ""))
            if st.form_submit_button("Guardar cambios del registro", type="primary"):
                editar_pilada(
                    sel_id, e_cliente, e_quintales, e_monto, e_saldo, e_estado, e_nota
                )
                st.success("Registro actualizado.")
                st.rerun()

        with st.expander("🗑️ Eliminar registro"):
            restar_maq = st.checkbox(
                "Restar también estos sacos al contador de aceite de la máquina",
                value=True,
            )
            if st.button("Eliminar registro seleccionado", type="secondary"):
                eliminar_pilada(sel_id, ajustar_maquina=restar_maq)
                st.warning("Registro eliminado.")
                st.rerun()

# --- TAB 5: CAMBIO DE ACEITE ---
with tab5:
    st.subheader("Registrar cambio de aceite")
    st.write(
        f"Sacos acumulados actualmente: {acumulados:g} | Cambios realizados en"
        f" total: {len(df_aceite)}"
    )
    with st.form("form_aceite", clear_on_submit=True):
        obs = st.text_input("Observación", value="Cambio de aceite realizado")
        reiniciar = st.form_submit_button(
            "Marcar cambio de aceite y poner contador en 0", type="primary"
        )
        if reiniciar:
            registrar_cambio_aceite(obs)
            st.success(
                "Cambio de aceite registrado. Contador reiniciado a 0."
            )
            st.rerun()

    if not df_aceite.empty:
        st.markdown("### Historial de cambios de aceite")
        st.dataframe(df_aceite, use_container_width=True, hide_index=True)
