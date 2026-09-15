# LiteX Refund Tracker — Design System

Derived from the **LiteX Portal** (https://portal.mylitex.com), an Ant Design + React
application. Tokens were extracted from the saved snapshot
`LiteX Portal.html` and its compiled stylesheet
`LiteX Portal_files/index-7yRF76qu.css`, plus the brand logo
`LiteX Portal_files/logo-Dk5ptieH.svg`.

This document is the **source of truth** for the visual restyle of the Streamlit
Refund Tracker. It documents presentation only — no application logic, data flow,
or tab structure was changed.

---

## 1. Typography

| Token            | Value                                             | Usage                                   |
|------------------|---------------------------------------------------|-----------------------------------------|
| Primary family   | **Outfit**, sans-serif                            | All text (body, headings, controls)     |
| Fallback stack   | `Outfit, -apple-system, "Segoe UI", Roboto, sans-serif` | When Outfit fails to load         |
| Weights loaded   | 300 / 400 / 500 / 600 / 700                       | 400 body, 500 controls, 600–700 titles  |

The portal ships a Vietnamese-tuned build (`SVN-Outfit`). We load the public
**Outfit** family from Google Fonts, matching the brand's chosen typeface:

```css
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');
```

Modal / heading treatment observed in the portal: title `22px`, line-height `29px`,
weight `600`, color `#212121`.

---

## 2. Color palette

### Brand / interactive (blue–cyan)

| Token                | Hex        | Where used                                                       |
|----------------------|------------|------------------------------------------------------------------|
| **Primary action**   | `#0D87E1`  | Buttons, active tab underline, checkboxes, focus borders, links  |
| Primary hover shadow | `#0C83DF66`| Button hover glow (`0 8px 20px 0 #0C83DF66`)                     |
| Focus / hover blue   | `#34A6ED`  | Softer hover accent                                              |
| Bright accent        | `#05C3FF`  | Cyan highlight (`rgb(5,195,255)`)                               |
| Deep brand blue      | `#004DBF`  | Left stop of the LiteX gradient                                 |
| Sky brand blue       | `#18BCFF`  | Right stop of the LiteX gradient                                |
| Link blue            | `#1677FF`  | Inline links (Ant default)                                      |
| Active / darker      | `#0958D9` / `#0267BA`  | Pressed states                                      |

**Signature gradients** (from the portal's Tailwind config):

- LiteX button:  `linear-gradient(90deg, #004DBF, #18BCFF)`  — `.bg-litex-button`
- LiteX header:  `linear-gradient(90deg, #18BCFF, #004DBF)`  — `.bg-litex-header`
- LiteX wordmark (text clip): `linear-gradient(90deg, #004DBF, #18BCFF)` — `.title-linear-litex`

### Neutrals / text

| Token              | Hex        | Usage                                     |
|--------------------|------------|-------------------------------------------|
| Text primary       | `#212121`  | Body copy, headings, metric values        |
| Text secondary     | `#454545`  | Secondary labels                          |
| Text tertiary      | `#7A7A7A`  | De-emphasised text                        |
| Muted / placeholder| `#888888`  | Captions, placeholders, metric labels     |
| Border             | `#E8E8E8`  | Card, input, table borders                |
| Border subtle      | `#F0F1F3`  | Hairline dividers                         |
| Surface            | `#FFFFFF`  | Cards, inputs, dropzone, dataframes       |
| App background     | `#F5F6FA`  | Page backdrop (portal body is `#F2F1F7`)  |
| Hover fill (blue)  | `#0D87E11A`| 10% primary tint on hover                 |

### Status

| Token    | Hex        | Bg tint    | Usage                        |
|----------|------------|------------|------------------------------|
| Error    | `#F32B2B`  | `#F32B2B1A`| `st.error`, failure states   |
| Error alt| `#EF4444`  | —          | Alt red (Tailwind red-500)   |
| Warning  | `#FF9900`  | `#FEF3C7`  | `st.warning`                 |
| Warning txt | `#D97706`| —          | Warning text                 |
| Success  | `#00BF36`  | `#0d87e11a`| `st.success`                 |
| Info     | `#0D87E1`  | `#E6F4FF`  | `st.info`                    |

---

## 3. Spacing

Follows an 8px-ish rhythm (Tailwind scale seen in the portal): 4, 8, 12, 16, 24, 32px.
Card padding is `16–24px`; control padding `~10–13px` vertical.

---

## 4. Radii

| Element                     | Radius  |
|-----------------------------|---------|
| Inputs, buttons             | `10px`  |
| Cards, metrics, dataframes  | `12px`  |
| Large cards / dropzone      | `15px`  |
| Modal / hero panels         | `20px`  |
| Pills / avatars             | `9999px`|

---

## 5. Shadows

| Token              | Value                              | Usage                    |
|--------------------|------------------------------------|--------------------------|
| Soft card          | `0 6px 16px rgba(0,0,0,0.08)`      | Metric cards, panels     |
| Portal card (alt)  | `6px 8px 40px -10px #0000001A`     | Large surfaces           |
| Button primary     | `0 8px 16px rgba(12,131,223,0.4)`  | Primary button rest      |
| Button hover glow  | `0 8px 20px 0 #0C83DF66`           | Primary button hover     |

---

## 6. Component styles (Streamlit mapping)

| Component                         | Streamlit selector                          | Treatment                                                                 |
|-----------------------------------|---------------------------------------------|---------------------------------------------------------------------------|
| Primary button                    | `.stButton > button[kind="primary"]`        | Gradient fill `#004DBF→#18BCFF`, white text, radius 10px, hover glow       |
| Secondary / download button       | `.stButton > button`, `.stDownloadButton > button` | White surface, `#0D87E1` text + border, radius 10px, blue tint on hover |
| Tabs                              | `.stTabs [data-baseweb="tab"]`              | Muted text; active tab `#0D87E1` text + 2px `#0D87E1` underline           |
| Metric card                       | `[data-testid="stMetric"]`                  | White surface, 1px `#E8E8E8` border, radius 12px, soft shadow, padding     |
| Metric label / value              | `[data-testid="stMetricLabel"/"stMetricValue"]` | Label `#888888`; value `#212121` weight 600                          |
| File uploader dropzone            | `[data-testid="stFileUploaderDropzone"]`    | White, dashed `#E8E8E8` border, radius 15px; hover border `#0D87E1`        |
| Text / number inputs              | `.stTextInput input`, `.stNumberInput input`| White, 1px `#E8E8E8`, radius 10px; focus border `#0D87E1`                  |
| Dataframe                         | `[data-testid="stDataFrame"]`               | 1px `#E8E8E8` border, radius 12px, clipped corners                         |
| Alerts (info/success/warn/error)  | `[data-testid="stAlert"]` (role-scoped)     | Tinted background + accent left color per status token                     |
| Progress bar                      | `.stProgress > div > div > div`             | Gradient fill `#004DBF→#18BCFF`                                            |
| Headings (h1–h3, subheader)       | `h1,h2,h3`                                   | `#212121`, Outfit, weight 600                                             |
| App header (custom)               | `.litex-header` (injected)                  | Gradient wordmark, app title, subtle `#E8E8E8` divider                    |

---

## 7. What could not be extracted

- **Exact Ant Design `colorPrimary` seed token** — Ant's runtime theme tokens live in
  the compiled JS bundle (`index-0JE9fleD.js`), not the CSS. The `#0D87E1` primary was
  inferred from the concrete component classes actually applied in the portal
  (checkbox/switch fill, active tab, hover borders), which is the effective brand blue.
- **Icon set** — the portal uses Ant + custom SVG icons; these were not ported. The
  Streamlit app keeps its emoji glyphs.
- The portal logo (`logo-Dk5ptieH.svg`) is a **white** wordmark meant for colored
  backgrounds. Rather than hotlink or recolor it, the app header uses a CSS wordmark
  with the portal's own `title-linear-litex` gradient treatment, which reads correctly
  on the light app background.
