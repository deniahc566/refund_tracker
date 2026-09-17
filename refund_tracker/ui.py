"""Presentation layer for the LiteX Refund Tracker Streamlit app.

Restyles the stock Streamlit UI to match the LiteX Portal design system
(see ``DESIGN_SYSTEM.md`` at the repo root). This module is PRESENTATION ONLY:
it exposes two helpers that inject CSS and render a branded header. It contains
no application logic, data access, or state.

Public API:
    inject_theme()  -> inject the design-system CSS (call once, right after
                       ``st.set_page_config``).
    header(...)     -> render the branded top header (title + gradient wordmark
                       + subtle divider) in place of a plain ``st.title``.
"""
from __future__ import annotations

import base64
import calendar as _cal
from datetime import date, timedelta
from functools import lru_cache
from html import escape

import streamlit as st

from . import config


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    """The LiteX logo as a base64 data URI (read once), or '' if unavailable."""
    try:
        return "data:image/png;base64," + base64.b64encode(
            config.LOGO_PATH.read_bytes()).decode()
    except Exception:
        return ""

# --- Design tokens (mirrors DESIGN_SYSTEM.md) ------------------------------
PRIMARY = "#0D87E1"          # brand azure — buttons, tabs, focus
GRAD_L = "#004DBF"           # gradient left stop
GRAD_R = "#18BCFF"           # gradient right stop
TEXT = "#212121"
MUTED = "#888888"
BORDER = "#E8E8E8"
SURFACE = "#FFFFFF"
APP_BG = "#F5F6FA"
RADIUS_INPUT = "10px"
RADIUS_CARD = "12px"
RADIUS_LG = "15px"
SHADOW_SOFT = "0 6px 16px rgba(0,0,0,0.08)"
SHADOW_BTN_HOVER = "0 8px 20px 0 #0C83DF66"

FONT_STACK = "'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
# Headings use a system stack (no Outfit): Outfit's Vietnamese diacritics render
# poorly at bold heading weights, so titles get a font with full VN coverage
# (Segoe UI on Windows), matching how body/label text renders.
HEAD_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif"

# Status tokens
ERROR = "#F32B2B"
WARNING = "#FF9900"
SUCCESS = "#00BF36"
INFO = PRIMARY


_THEME_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');

/* ---- Global type + backdrop ---------------------------------------- */
html, body, [class*="css"], .stApp,
[data-testid="stAppViewContainer"], [data-testid="stSidebar"] {{
    font-family: {FONT_STACK} !important;
}}
.stApp {{
    background-color: {APP_BG};
    color: {TEXT};
}}
[data-testid="stAppViewContainer"] {{
    background-color: {APP_BG};
}}
[data-testid="stHeader"] {{
    background: transparent;
}}

/* Headings */
h1, h2, h3, h4,
[data-testid="stHeading"] {{
    font-family: {HEAD_FONT} !important;
    color: {TEXT};
    font-weight: 600;
    letter-spacing: -0.2px;
}}
[data-testid="stCaptionContainer"], .stCaption {{
    color: {MUTED} !important;
}}

/* ---- Branded header ------------------------------------------------- */
.litex-header {{
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 8px 0 16px 0;
    margin-bottom: 8px;
    border-bottom: 1px solid {BORDER};
}}
.litex-mark {{
    font-size: 30px;
    line-height: 1;
    filter: drop-shadow(0 2px 6px rgba(12,131,223,0.25));
}}
.litex-logo {{
    height: 40px;
    width: auto;
    display: block;
}}
.litex-wordmark {{
    font-family: {FONT_STACK};
    font-weight: 700;
    font-size: 26px;
    line-height: 1;
    background: linear-gradient(90deg, {GRAD_L}, {GRAD_R});
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: 0.3px;
}}
.litex-title-block {{
    display: flex;
    flex-direction: column;
    gap: 2px;
}}
.litex-title {{
    font-family: {HEAD_FONT};
    font-weight: 600;
    font-size: 20px;
    color: {TEXT};
    line-height: 1.2;
}}
.litex-subtitle {{
    font-family: {FONT_STACK};
    font-size: 13px;
    color: {MUTED};
    line-height: 1.2;
}}
.litex-divider-dot {{
    width: 1px;
    align-self: stretch;
    background: {BORDER};
    margin: 2px 4px;
}}

/* ---- Buttons -------------------------------------------------------- */
.stButton > button, .stDownloadButton > button {{
    font-family: {FONT_STACK} !important;
    font-weight: 500;
    border-radius: {RADIUS_INPUT};
    border: 1px solid {BORDER};
    color: {PRIMARY};
    background: {SURFACE};
    transition: all 0.15s cubic-bezier(.4,0,.2,1);
    box-shadow: none;
}}
.stButton > button:hover, .stDownloadButton > button:hover {{
    border-color: {PRIMARY};
    color: {PRIMARY};
    background: #0D87E10D;
}}
.stButton > button:active, .stDownloadButton > button:active {{
    background: #0D87E11A;
}}
/* Primary button — LiteX gradient fill */
.stButton > button[kind="primary"],
.stButton > button[data-testid="baseButton-primary"] {{
    background-image: linear-gradient(90deg, {GRAD_L}, {GRAD_R});
    color: #FFFFFF;
    border: none;
    box-shadow: 0 6px 16px rgba(12,131,223,0.28);
}}
.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="baseButton-primary"]:hover {{
    color: #FFFFFF;
    box-shadow: {SHADOW_BTN_HOVER};
    filter: brightness(1.02);
}}
.stButton > button[kind="primary"]:disabled,
.stButton > button[kind="primary"][disabled] {{
    background-image: none;
    background-color: #CCCCCC;
    color: #FFFFFF;
    box-shadow: none;
    opacity: 0.7;
}}
/* Download button gets the primary gradient treatment too */
.stDownloadButton > button {{
    background-image: linear-gradient(90deg, {GRAD_L}, {GRAD_R});
    color: #FFFFFF;
    border: none;
    box-shadow: 0 6px 16px rgba(12,131,223,0.28);
}}
.stDownloadButton > button:hover {{
    color: #FFFFFF;
    background: none;
    background-image: linear-gradient(90deg, {GRAD_L}, {GRAD_R});
    box-shadow: {SHADOW_BTN_HOVER};
}}

/* ---- Tabs ----------------------------------------------------------- */
.stTabs [data-baseweb="tab-list"] {{
    gap: 6px;
    border-bottom: 1px solid {BORDER};
}}
.stTabs [data-baseweb="tab"] {{
    font-family: {FONT_STACK} !important;
    font-weight: 500;
    color: {MUTED};
    background: transparent;
    padding: 8px 14px;
}}
.stTabs [data-baseweb="tab"]:hover {{
    color: {TEXT};
    background: #0D87E10D;
    border-radius: {RADIUS_INPUT} {RADIUS_INPUT} 0 0;
}}
.stTabs [aria-selected="true"] {{
    color: {PRIMARY} !important;
}}
.stTabs [data-baseweb="tab-highlight"] {{
    background-color: {PRIMARY};
    height: 2px;
}}
.stTabs [data-baseweb="tab-border"] {{
    background-color: {BORDER};
}}

/* ---- Metric cards --------------------------------------------------- */
[data-testid="stMetric"] {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD};
    padding: 16px 18px;
    box-shadow: {SHADOW_SOFT};
}}
[data-testid="stMetricLabel"] {{
    color: {MUTED} !important;
}}
[data-testid="stMetricLabel"] p {{
    font-weight: 500;
    color: {MUTED} !important;
}}
[data-testid="stMetricValue"] {{
    color: {TEXT};
    font-weight: 600;
}}

/* ---- File uploader dropzone ---------------------------------------- */
[data-testid="stFileUploaderDropzone"] {{
    background: {SURFACE};
    border: 1.5px dashed {BORDER};
    border-radius: {RADIUS_LG};
    transition: border-color 0.15s ease, background 0.15s ease;
}}
[data-testid="stFileUploaderDropzone"]:hover {{
    border-color: {PRIMARY};
    background: #0D87E10D;
}}
[data-testid="stFileUploaderDropzone"] button {{
    font-family: {FONT_STACK} !important;
    border-radius: {RADIUS_INPUT};
    border: 1px solid {BORDER};
    color: {PRIMARY};
    font-weight: 500;
}}
[data-testid="stFileUploaderDropzone"] button:hover {{
    border-color: {PRIMARY};
    background: #0D87E11A;
}}
/* Uploaded file chips */
[data-testid="stFileUploaderFile"] {{
    background: #F5F6FA;
    border-radius: {RADIUS_INPUT};
}}

/* ---- Inputs --------------------------------------------------------- */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
[data-baseweb="input"], [data-baseweb="textarea"] {{
    border-radius: {RADIUS_INPUT} !important;
    font-family: {FONT_STACK} !important;
}}
.stTextInput div[data-baseweb="input"],
.stNumberInput div[data-baseweb="input"] {{
    border-radius: {RADIUS_INPUT};
    border-color: {BORDER};
}}
.stTextInput div[data-baseweb="input"]:focus-within,
.stNumberInput div[data-baseweb="input"]:focus-within {{
    border-color: {PRIMARY};
    box-shadow: 0 0 0 2px #0D87E133;
}}

/* ---- Dataframes ----------------------------------------------------- */
[data-testid="stDataFrame"] {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD};
    overflow: hidden;
}}
[data-testid="stDataFrame"] [data-testid="stTable"] {{
    border-radius: {RADIUS_CARD};
}}

/* ---- Progress bar --------------------------------------------------- */
.stProgress > div > div > div > div {{
    background-image: linear-gradient(90deg, {GRAD_L}, {GRAD_R});
}}

/* ---- Alerts --------------------------------------------------------- */
[data-testid="stAlert"] {{
    border-radius: {RADIUS_CARD};
    font-family: {FONT_STACK} !important;
    border: 1px solid transparent;
}}
/* Info */
[data-testid="stAlert"][data-baseweb="notification"] {{
    box-shadow: none;
}}

/* ---- Status / expander (st.status) --------------------------------- */
[data-testid="stExpander"] {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CARD};
    background: {SURFACE};
}}
[data-testid="stExpander"] summary {{
    font-family: {FONT_STACK} !important;
}}

/* ---- Subheaders / markdown ----------------------------------------- */
.stMarkdown, .stMarkdown p {{
    font-family: {FONT_STACK} !important;
}}
</style>
"""


def inject_theme() -> None:
    """Inject the LiteX Portal design-system CSS.

    Call once, immediately after ``st.set_page_config(...)``. Loads the Outfit
    font, sets the app background, and restyles buttons, tabs, metric cards,
    inputs, the file-uploader dropzone, dataframes, progress bars, and alerts to
    match the portal. A light appearance is pinned via ``.streamlit/config.toml``
    (``base = "light"``), so no dark-mode overrides are needed.
    """
    st.markdown(_THEME_CSS, unsafe_allow_html=True)


def header(title: str = "Refund Tracker") -> None:
    """Render the branded LiteX top header.

    A gradient ``LiteX`` wordmark (matching the portal's ``title-linear-litex``
    treatment) sits beside the app title, above a subtle divider. This replaces
    the plain ``st.title`` / ``st.caption`` pairing.

    Args:
        title: the app title shown next to the wordmark.
    """
    uri = _logo_data_uri()
    brand = (f'<img class="litex-logo" src="{uri}" alt="LiteX">' if uri
             else '<span class="litex-wordmark">LiteX</span>')
    st.markdown(
        f"""
        <div class="litex-header">
            {brand}
            <span class="litex-divider-dot"></span>
            <span class="litex-title-block">
                <span class="litex-title">{title}</span>
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --- Contribution-style heatmap (GitHub look) ------------------------------
# Empty day is neutral gray; the 4 filled levels ramp light -> dark. Blue for
# transactions (LiteX brand), red for duplicates (warning).
_HEAT_BLUE = ["#ebedf0", "#cfe9fb", "#86c8f0", "#2f9be0", "#0d6fb8"]
_HEAT_RED = ["#ebedf0", "#fde0e0", "#f5a3a3", "#ef5f5f", "#d61f1f"]
_WEEKDAY_LABELS = {1: "T2", 3: "T4", 5: "T6"}  # Mon / Wed / Fri rows (Sun-first)
_MONTHS_VN = ["", "Th1", "Th2", "Th3", "Th4", "Th5", "Th6",
              "Th7", "Th8", "Th9", "Th10", "Th11", "Th12"]


def heatmap_html(daily: dict, year: int, metric: str = "txn") -> str:
    """GitHub-style year heatmap. `daily` maps 'YYYY-MM-DD' -> (txns, dups);
    `metric` is 'txn' or 'dup' (chooses the colored value). The tooltip always
    shows both counts. Weeks are columns (Sunday-first), weekdays are rows."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    first_sunday = start - timedelta(days=(start.weekday() + 1) % 7)
    ramp = _HEAT_RED if metric == "dup" else _HEAT_BLUE

    max_v = 0
    for t, dp in daily.values():
        max_v = max(max_v, dp if metric == "dup" else t)

    def level(v: int) -> int:
        if v <= 0 or max_v <= 0:
            return 0
        r = v / max_v
        return 1 if r <= 0.25 else 2 if r <= 0.5 else 3 if r <= 0.75 else 4

    squares, month_labels = [], []
    seen_months = set()
    d = start
    while d <= end:
        col = (d - first_sunday).days // 7
        row = (d.weekday() + 1) % 7  # Sun=0 .. Sat=6
        t, dp = daily.get(d.isoformat(), (0, 0))
        v = dp if metric == "dup" else t
        color = ramp[level(v)]
        title = escape(f"{d.strftime('%d/%m/%Y')}: {t:,} giao dịch · {dp:,} trùng")
        squares.append(
            f'<div title="{title}" style="grid-row:{row + 1};grid-column:{col + 1};'
            f'width:12px;height:12px;border-radius:3px;background:{color}"></div>'
        )
        if d.month not in seen_months:  # month label at the week its 1st lands
            seen_months.add(d.month)
            month_labels.append(
                f'<div style="grid-column:{col + 1};grid-row:1;font-size:10px;'
                f'color:{MUTED}">{_MONTHS_VN[d.month]}</div>'
            )
        d += timedelta(days=1)
    n_cols = (end - first_sunday).days // 7 + 1

    wk_labels = "".join(
        f'<div style="grid-row:{r + 1};grid-column:1;font-size:10px;color:{MUTED};'
        f'line-height:12px">{lbl}</div>'
        for r, lbl in _WEEKDAY_LABELS.items()
    )
    legend = "".join(
        f'<span style="width:12px;height:12px;border-radius:3px;background:{c};'
        f'display:inline-block"></span>' for c in ramp
    )
    return f"""
    <div style="overflow-x:auto;padding:4px 0">
      <div style="display:grid;grid-template-columns:repeat({n_cols},12px);
                  gap:3px;margin-bottom:2px;margin-left:22px">{''.join(month_labels)}</div>
      <div style="display:flex;gap:4px;align-items:start">
        <div style="display:grid;grid-template-rows:repeat(7,12px);gap:3px;width:18px">{wk_labels}</div>
        <div style="display:grid;grid-template-rows:repeat(7,12px);
                    grid-template-columns:repeat({n_cols},12px);gap:3px;
                    grid-auto-flow:column">{''.join(squares)}</div>
      </div>
      <div style="display:flex;gap:4px;align-items:center;justify-content:flex-end;
                  margin-top:6px;font-size:11px;color:{MUTED}">
        Ít {legend} Nhiều
      </div>
    </div>
    """
