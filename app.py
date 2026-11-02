import io
import json
import os
import re
import shutil
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # підхоплює .env з поточної папки (де запускаєте streamlit)

BASE_URL = "https://apiua.omprice.com"
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)

ROW_COLUMNS = ["Наш артикул", "Артикул Omprice", "Дата", "Ціна промо",
               "Ціна не промо", "Мережа", "Магазин"]


# ───────────────────────── API ─────────────────────────
class ApiClient:
    def __init__(self, token: str):
        self.token = token
        self.sess = requests.Session()

    @staticmethod
    def _unwrap(r):
        if not r.ok:  # URL з токеном у помилку не потрапляє
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if not body.get("success", False):
            raise RuntimeError(body.get("message", body))
        return body["data"]

    def get(self, path, **params):
        r = self.sess.get(f"{BASE_URL}{path}", params={"token": self.token, **params}, timeout=60)
        return self._unwrap(r)

    def post(self, path, payload):
        r = self.sess.post(f"{BASE_URL}{path}", params={"token": self.token}, json=payload, timeout=60)
        return self._unwrap(r)

    def download_json(self, job):
        file_name = job["file"].replace("\\", "/")
        ext = job.get("details", {}).get("extension", ".json")
        query = urlencode({"filename": f"{file_name}{ext}", "token": self.token}, safe="/")
        url = f"{BASE_URL}/api/v1/data/{file_name}?{query}"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            with self.sess.get(url, stream=True, timeout=600) as r:
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code} під час завантаження файлу")
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        finally:
            path.unlink(missing_ok=True)


@st.cache_data(ttl=3600, show_spinner="Завантажую список магазинів…")
def load_shops(_token: str) -> pd.DataFrame:
    data = ApiClient(_token).get("/api/v1/export/shops-list/")
    return pd.DataFrame([{
        "id": s["_id"],
        "network": s.get("network") or "—",
        "name": s.get("name") or s["_id"],
    } for s in data])


def parse_articles(text: str) -> list[str]:
    tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
    # заголовок з Excel (напр. «артикул»), якщо решта — числа
    if len(tokens) > 1 and not tokens[0].isdigit() and all(t.isdigit() for t in tokens[1:]):
        tokens = tokens[1:]
    return list(dict.fromkeys(tokens))


def fetch_day(client, day, shops, articles, shops_info, poll, timeout, on_state):
    payload = {"date": day, "shops": list(shops), "articles": list(articles)}
    job_id = client.post("/api/v1/export/customer-api/prices", payload)["_id"]
    t0 = time.time()
    while True:
        job = client.get(f"/api/v1/export/get/{job_id}/")
        on_state(job["state"], int(time.time() - t0))
        if job["state"] == "complete":
            break
        if time.time() - t0 > timeout:
            raise TimeoutError(f"state={job['state']}")
        time.sleep(poll)

    mine = set(articles)
    rows = []
    for item in client.download_json(job):
        for custom in item.get("articleCustom") or []:
            if custom not in mine:
                continue
            for p in item.get("pricesOfProduct", []):
                promo = bool(p.get("isPromo"))
                price, old = p.get("price"), p.get("oldPrice")
                sh = shops_info.get(p.get("shop"), {})
                rows.append({
                    "shop_id": p.get("shop"),
                    "Наш артикул": custom,
                    "Артикул Omprice": item.get("article"),
                    "Дата": day,
                    "Ціна промо": price if promo else None,
                    "Ціна не промо": old if promo else price,
                    "Мережа": sh.get("network"),
                    "Магазин": sh.get("name", p.get("shop")),
                })
    return rows


# ── Кеш по днях ──
# Файл .cache/<день>.json: {"rows": [...], "covered": {shop_id: [артикули, які вже запитували]}}.
# «Покрито» = пара (магазин, артикул) уже запитана за цей день; навіть якщо промо не було —
# повторно це не питаємо. Інший набір артикулів довантажує лише те, чого бракує.
def _load_day_cache(day):
    p = CACHE_DIR / f"{day}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"rows": [], "covered": {}}


def get_day_rows(client, day, shops, articles, shops_info, poll, timeout, on_state, use_cache):
    # Повертає (рядки для вибраних магазинів і артикулів, скільки пар довантажено з API, скільки було в кеші)
    cacheable = use_cache and date.fromisoformat(day) < date.today()   # сьогодні не кешуємо: дані ще змінюються
    cache = _load_day_cache(day) if cacheable else {"rows": [], "covered": {}}
    covered = {sid: set(arts) for sid, arts in cache["covered"].items()}

    missing = [(sid, a) for sid in shops for a in articles if a not in covered.get(sid, set())]
    total = len(shops) * len(articles)
    rows_all = cache["rows"]

    if missing:
        req_shops = sorted({sid for sid, _ in missing})
        req_arts = sorted({a for _, a in missing})
        new_rows = fetch_day(client, day, req_shops, req_arts, shops_info, poll, timeout, on_state)
        requested = {(sid, a) for sid in req_shops for a in req_arts}
        rows_all = [r for r in rows_all if (r["shop_id"], r["Наш артикул"]) not in requested] + new_rows
        for sid in req_shops:
            covered.setdefault(sid, set()).update(req_arts)
        if cacheable:
            CACHE_DIR.mkdir(exist_ok=True)
            (CACHE_DIR / f"{day}.json").write_text(json.dumps(
                {"rows": rows_all, "covered": {k: sorted(v) for k, v in covered.items()}},
                ensure_ascii=False), encoding="utf-8")

    shopset, artset = set(shops), set(articles)
    result = [r for r in rows_all if r["shop_id"] in shopset and r["Наш артикул"] in artset]
    return result, len(missing), total - len(missing)


# ───────────────────────── Зведена ─────────────────────────
def _art(a: str):
    # артикул як число (щоб Excel не ставив «число як текст»), але не чіпаємо коди з нулями на початку
    return int(a) if a.isdigit() and not a.startswith("0") else a


def build_monitoring(raw: pd.DataFrame, articles: list[str], maudau_pattern: str,
                     exclude_networks: list[str], top_n: int = 2):
    promo = raw[raw["Ціна промо"].notna()].copy()
    promo["Дата"] = pd.to_datetime(promo["Дата"])

    if exclude_networks:
        promo = promo[~promo["Мережа"].fillna("").isin(exclude_networks)]

    # найнижчі ціни вгору, при рівних цінах — найсвіжіша дата
    promo = promo.sort_values(["Ціна промо", "Дата"], ascending=[True, False])
    # один запис на «артикул + мережа» (найнижча промо-ціна за період)
    promo = promo.drop_duplicates(["Наш артикул", "Мережа"], keep="first")

    if maudau_pattern:
        is_maudau = promo["Мережа"].fillna("").str.contains(maudau_pattern, case=False, regex=False)
    else:
        is_maudau = pd.Series(False, index=promo.index)
    maudau_df, other = promo[is_maudau], promo[~is_maudau]

    # конкуренти: нумерація всередині артикулу від найдешевшого (аналог COUNTIF)
    other = other.sort_values(["Наш артикул", "Ціна промо", "Дата"],
                              ascending=[True, True, False]).copy()
    other["Num"] = other.groupby("Наш артикул").cumcount() + 1

    # maudau: найнижча промо-ціна по артикулу
    maudau_best = (maudau_df.sort_values(["Ціна промо", "Дата"], ascending=[True, False])
                   .drop_duplicates("Наш артикул").set_index("Наш артикул"))

    comp = {(r["Наш артикул"], r["Num"]): r for r in other.to_dict("records") if r["Num"] <= top_n}

    rows = []
    for art in articles:                      # усі введені артикули, у порядку введення
        row = {"Артикул": _art(art)}
        for n in range(1, top_n + 1):
            r = comp.get((art, n))
            row[f"Ціна конкурента {n}"] = r["Ціна промо"] if r else None
            row[f"Дата конкурента {n}"] = r["Дата"].date() if r else None
            row[f"Конкурент {n}"] = r["Мережа"] if r else None
        if art in maudau_best.index:
            m = maudau_best.loc[art]
            row["Ціна конкурента M"] = m["Ціна промо"]
            row["Дата конкурента M"] = m["Дата"].date()
        else:
            row["Ціна конкурента M"] = None
            row["Дата конкурента M"] = None
        rows.append(row)
    pivot = pd.DataFrame(rows)

    # довідковий аркуш: усі мережі після очищення (не лише топ-N)
    clean = pd.concat([
        other.assign(Група="Конкурент"),
        maudau_df.assign(Група="Maudau", Num=None),
    ])[["Наш артикул", "Група", "Num", "Мережа", "Магазин", "Ціна промо", "Дата"]]
    clean = clean.sort_values(["Наш артикул", "Група", "Ціна промо"]).reset_index(drop=True)
    clean["Дата"] = clean["Дата"].dt.date

    return {"pivot": pivot, "clean": clean,
            "promo_articles": set(promo["Наш артикул"]),
            "maudau_count": int(pivot["Ціна конкурента M"].notna().sum())}


def to_excel(res) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl", date_format="DD.MM.YYYY") as xw:
        for name, df in [("Промо моніторинг", res["pivot"]), ("Очищені дані", res["clean"])]:
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "B2"
            for col in ws.columns:
                if str(col[0].value).startswith("Дата"):
                    for c in col[1:]:
                        c.number_format = "DD.MM.YYYY"
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 12), 45)
    return buf.getvalue()


# ───────────────────────── UI ─────────────────────────
st.set_page_config(page_title="Промо моніторинг", page_icon="🏷️", layout="wide")
st.title("🏷️ Промо моніторинг")

token = os.environ.get("API_KEY") or os.environ.get("KEY")
if not token:
    st.error("Не знайдено токен: додайте `API_KEY=...` у файл `.env` поруч із `app.py` (або в змінні середовища) і перезапустіть застосунок.")
    st.stop()

try:
    shops_df = load_shops(token)
except Exception as e:
    st.error(f"Не вдалося отримати список магазинів: {e}")
    st.stop()

shops_info = {r.id: {"network": r.network, "name": r.name} for r in shops_df.itertuples()}

with st.sidebar:
    st.header("Налаштування")
    maudau_pattern = st.text_input("Назва мережі «maudau» (містить)", value="maudau")
    exclude_networks = st.multiselect(
        "Виключити мережі зі зведеної", sorted(shops_df["network"].unique()),
        help="Наприклад, власна мережа, якщо вона є серед вибраних філій.")
    use_cache = st.checkbox("Кешувати завершені дні на диск", value=True,
                            help="Кеш працює по парах «день + магазин + артикул»: інший список артикулів "
                                 "довантажує лише нові. Сьогоднішній день не кешується.")
    if st.button("🗑️ Очистити кеш"):
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        st.success("Кеш очищено")
    top_n = st.number_input("Скільки конкурентів показувати (без maudau)", 1, 10, 2,
                            help="Беруться N найдешевших промо-цін від різних мереж.")
    poll = st.number_input("Інтервал опитування статусу, с", 5, 120, 15)

col_a, col_b = st.columns([1, 1])
with col_a:
    articles_text = st.text_area("Наші кастомні артикули (вставте з буфера: Ctrl+V)", height=220,
                                 placeholder="365571\n365572\n519087\n…")
    articles = parse_articles(articles_text)
    st.caption(f"Розпізнано артикулів: **{len(articles)}**")

with col_b:
    today = date.today()
    period = st.date_input("Період", value=(today - timedelta(days=35), today), max_value=today,
                           format="DD.MM.YYYY")
    d0, d1 = (period[0], period[-1]) if isinstance(period, (tuple, list)) and period else (period, period)
    n_days = (d1 - d0).days + 1
    st.caption(f"Днів у періоді: **{n_days}** — кожен день це окремий звіт, вони йдуть по черзі.")

    options = shops_df.sort_values(["network", "name"])["id"].tolist()
    labels = {r.id: f"{r.network} — {r.name}" for r in shops_df.itertuples()}
    if "shops_sel" not in st.session_state:
        st.session_state["shops_sel"] = list(options)      # за замовчуванням вибрані всі

    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("Вибрати всі", width="stretch"):
        st.session_state["shops_sel"] = list(options)
    if b2.button("Зняти всі", width="stretch"):
        st.session_state["shops_sel"] = []

    selected = st.multiselect("Магазини (список з API)", options, key="shops_sel",
                              format_func=lambda i: labels[i])
    st.caption(f"Вибрано магазинів: **{len(selected)}** з {len(options)}")

run = st.button("▶️ Запустити моніторинг", type="primary", disabled=not (articles and selected))

if run:
    client = ApiClient(token)
    all_rows, failed = [], []
    days = [d0 + timedelta(days=i) for i in range(n_days)]
    bar = st.progress(0.0)
    status = st.empty()

    from_cache = from_api = 0
    for i, d in enumerate(days):
        day = d.isoformat()
        try:
            def on_state(state, secs, day=day, i=i):
                status.info(f"[{i + 1}/{n_days}] {day}: запит до API — {state} ({secs} с)")
            rows, n_api, n_cache = get_day_rows(client, day, selected, articles, shops_info,
                                                poll, 3 * 3600, on_state, use_cache)
            from_api += n_api
            from_cache += n_cache
            all_rows.extend(rows)
        except Exception as e:
            failed.append((day, str(e)))
        bar.progress((i + 1) / n_days)
    status.empty()
    if from_cache + from_api:
        st.caption(f"Пар «магазин × артикул × день»: з кешу {from_cache}, з API {from_api}.")

    raw = pd.DataFrame(all_rows, columns=ROW_COLUMNS)
    for c in ("Ціна промо", "Ціна не промо"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")

    st.session_state["result"] = {
        "raw": raw, "failed": failed, "articles": articles,
        "period": f"{d0:%Y-%m-%d}_{d1:%Y-%m-%d}",
    }

res_state = st.session_state.get("result")
if res_state:
    if res_state["failed"]:
        st.warning("Не завантажились дні: " + "; ".join(f"{d} ({e[:80]})" for d, e in res_state["failed"]))

    raw = res_state["raw"]
    res = build_monitoring(raw, res_state["articles"], maudau_pattern, exclude_networks, int(top_n))

    if raw.empty or not res["promo_articles"]:
        st.info("Промо-цін за вибраний період не знайдено.")
    else:
        no_promo = [a for a in res_state["articles"] if a not in res["promo_articles"]]
        c1, c2, c3 = st.columns(3)
        c1.metric("Артикулів з промо", len(res["promo_articles"]))
        c2.metric("Артикулів без промо", len(no_promo))
        c3.metric("Артикулів з промо у maudau", res["maudau_count"])

        st.download_button(
            "⬇️ Завантажити Excel", data=to_excel(res),
            file_name=f"promo_monitoring_{res_state['period']}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")

        t1, t2, t3 = st.tabs(["Промо моніторинг", "Очищені дані", "Сирі дані"])
        t1.dataframe(res["pivot"], width="stretch", hide_index=True)
        t2.dataframe(res["clean"], width="stretch", hide_index=True)
        t3.dataframe(raw, width="stretch", hide_index=True)
        if no_promo:
            with st.expander(f"Артикули без промо в періоді ({len(no_promo)})"):
                st.write(", ".join(no_promo))