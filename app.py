import os
import json
import time
import base64
import copy
import datetime
from datetime import timedelta
from typing import Optional, Dict, Any, Tuple
import requests
import urllib3
import urllib.parse
from typing import Any, List, Set
import io
import csv
import re
from functools import lru_cache
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    import pycountry  # type: ignore
except Exception:
    pycountry = None  # type: ignore

try:
    # PDF generation (optional dependency)
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
except Exception:
    A4 = None  # type: ignore
    landscape = None  # type: ignore
    inch = 72  # type: ignore
    ImageReader = None  # type: ignore
    rl_canvas = None  # type: ignore

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, Response, make_response, jsonify
)

GRAPH_ME_ENDPOINT = "https://graph.microsoft.com/v1.0/me"
ORG_NAME = "Company"

# Toggle SSL verification (set VERIFY_SSL=true when your machine trusts corp CA)
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() == "true"
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def graph_get_me(access_token: str):
    headers = {"Authorization": f"Bearer {access_token.strip()}"}
    resp = requests.get(GRAPH_ME_ENDPOINT, headers=headers, verify=VERIFY_SSL)
    return resp


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-please")
app.permanent_session_lifetime = timedelta(minutes=30)


# ------------------------------
# Background prefetch (warm caches after login)
# ------------------------------

def _employees_prime_default_cache(token: str) -> None:
    """Warm a small default employees query so the Employees page is fast on first open.

    Notes:
    - This caches a limited dataset (defaults used by /employees GET).
    - Cache is per-process and best-effort; failures are logged but don't break login.
    """
    try:
        headers = _graph_headers_from_token(token)
        selected_fields = ["displayName", "mail", "jobTitle", "department"]
        url = (
            "https://graph.microsoft.com/v1.0/users"
            "?$top=999"
            f"&$select={urllib.parse.quote(','.join(['id'] + selected_fields))}"
        )
        rows = _graph_get_paged(url, headers, max_items=500)

        cache = getattr(app, "_employees_cache", None)
        if cache is None:
            cache = {"ts": 0.0, "key": None, "rows": None}
            setattr(app, "_employees_cache", cache)
        cache["ts"] = time.time()
        cache["key"] = (tuple(selected_fields), "", "", "", "", "", 500)
        cache["rows"] = rows

        # Store prefetch count in session if possible (best effort)
        try:
            from flask import session
            session["prefetch_count"] = len(rows)
        except:
            pass
    except Exception:
        app.logger.exception("Employees prefetch failed")


def _prime_hierarchy_cache(token: str) -> None:
    """Warm the hierarchy dataset used by the hierarchy page."""
    try:
        headers = _graph_headers_from_token(token)
        # The hierarchy view needs id/displayName/manager relationships.
        # Keep this dataset modest but useful.
        _org_sim_fetch_users_with_manager(headers, max_items=0)
    except Exception:
        app.logger.exception("Hierarchy prefetch failed")


def _prime_profile_photo_cache(token: str) -> None:
    """Best-effort warm /me photo (helps dashboard feel instant)."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        requests.get("https://graph.microsoft.com/v1.0/me/photo/$value", headers=headers, verify=VERIFY_SSL, timeout=10)
    except Exception:
        # Ignore any errors (photo is optional)
        pass


def _kickoff_post_login_prefetch(token: str) -> None:
    """Start background prefetch tasks after sign-in.

    Runs in a daemon thread so it doesn't block the redirect to /dashboard.
    """
    if not token:
        return

    # Avoid spawning multiple prefetch jobs repeatedly for the same session.
    # We set a short-lived session flag; the actual caches are per-process.
    try:
        if session.get("prefetch_started"):
            return
        session["prefetch_started"] = True
    except Exception:
        pass

    def _run():
        start = time.time()
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [
                    ex.submit(insights_data_prefetch, token),
                    ex.submit(_employees_prime_default_cache, token),
                    ex.submit(_prime_hierarchy_cache, token),
                    ex.submit(_prime_profile_photo_cache, token),
                ]
                for f in as_completed(futs):
                    _ = f.result()
        except Exception:
            app.logger.exception("Post-login prefetch failed")
        finally:
            app.logger.info("Post-login prefetch done in %.2fs", time.time() - start)

    threading.Thread(target=_run, daemon=True).start()


def insights_data_prefetch(token: str) -> None:
    """Warm the /insights/data cache (same logic, without request context)."""
    headers = _graph_headers_from_token(token)
    cache = getattr(app, "_insights_cache", None)
    if cache is None:
        cache = {"ts": 0, "rows": None, "company": None, "company_ts": 0}
        setattr(app, "_insights_cache", cache)

    now = time.time()
    ttl_sec = 300
    if cache.get("rows") and (now - float(cache.get("ts") or 0) < ttl_sec):
        return

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_counts = ex.submit(_insights_fetch_dept_location_city_country_counts, headers, 0)
        f_users = ex.submit(_insights_fetch_users_min_fields, headers, 0)
        f_company = ex.submit(_insights_fetch_company_name, headers)
        dept_counts, loc_counts, city_counts, country_counts = f_counts.result()
        users_min = f_users.result()
        company_name = f_company.result() or ORG_NAME

    rows = [
        {"department": k, "count": int(v)}
        for k, v in sorted(dept_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        if k != "(Unassigned)"
    ]
    loc_rows = [
        {"location": k, "count": int(v)}
        for k, v in sorted(loc_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        if k != "(Unassigned)"
    ]
    city_rows = [
        {"city": k, "count": int(v)}
        for k, v in sorted(city_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        if k != "(Unassigned)"
    ]
    country_rows = [
        {"country": k, "count": int(v)}
        for k, v in sorted(country_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        if k != "(Unassigned)"
    ]
    cache["rows"] = rows
    cache["loc_rows"] = loc_rows
    cache["city_rows"] = city_rows
    cache["country_rows"] = country_rows
    cache["users_min"] = users_min
    cache["ts"] = now
    cache["company"] = company_name
    cache["company_ts"] = now


def _pdf_color(theme: str, what: str):
    """Very small theme palette for PDF generation."""
    theme = (theme or "").lower()
    dark = theme != "light"
    if what == "bg":
        return (0.04, 0.06, 0.13) if dark else (0.98, 0.98, 1.00)
    if what == "card":
        return (0.08, 0.10, 0.20) if dark else (1.00, 1.00, 1.00)
    if what == "stroke":
        return (0.20, 0.22, 0.30) if dark else (0.82, 0.84, 0.90)
    if what == "text":
        return (0.93, 0.95, 1.00) if dark else (0.08, 0.10, 0.16)
    if what == "muted":
        return (0.72, 0.76, 0.88) if dark else (0.38, 0.42, 0.52)
    return (1, 1, 1)


def _pdf_draw_card(c, x, y, w, h, theme: str):
    """Draw a rounded-ish card background (rectangle for simplicity)."""
    r, g, b = _pdf_color(theme, "card")
    c.setFillColorRGB(r, g, b)
    sr, sg, sb = _pdf_color(theme, "stroke")
    c.setStrokeColorRGB(sr, sg, sb)
    c.setLineWidth(1)
    c.rect(x, y, w, h, fill=1, stroke=1)


def _safe_b64_png_to_bytes(data_url: str) -> Optional[bytes]:
    if not data_url:
        return None
    try:
        if "," in data_url:
            head, b64 = data_url.split(",", 1)
        else:
            b64 = data_url
        return base64.b64decode(b64)
    except Exception:
        return None


@app.route("/insights/report.pdf", methods=["POST"])
def insights_report_pdf():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401

    if rl_canvas is None or A4 is None or ImageReader is None or landscape is None:
        return (
            {
                "ok": False,
                "error": "PDF export dependency missing. Install 'reportlab' in the server environment.",
            },
            500,
        )

    payload = request.get_json(silent=True) or {}
    company = str(payload.get("company") or ORG_NAME)
    theme = str(payload.get("theme") or "dark")
    filters = str(payload.get("filters") or "")
    generated_at = str(payload.get("generatedAt") or "")

    kpi = payload.get("kpi") or {}
    charts = payload.get("charts") or {}

    buf = io.BytesIO()
    # Use landscape orientation so the report fits comfortably on a single page.
    page_size = landscape(A4)
    c = rl_canvas.Canvas(buf, pagesize=page_size)
    page_w, page_h = page_size

    # Background
    br, bg, bb = _pdf_color(theme, "bg")
    def _pdf_paint_page_bg():
        c.setFillColorRGB(br, bg, bb)
        c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    _pdf_paint_page_bg()

    margin = 24
    x0 = margin
    y = page_h - margin

    # Header card
    header_h = 66
    _pdf_draw_card(c, x0, y - header_h, page_w - 2 * margin, header_h, theme)
    tr, tg, tb = _pdf_color(theme, "text")
    mr, mg, mb = _pdf_color(theme, "muted")
    c.setFillColorRGB(tr, tg, tb)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x0 + 14, y - 30, f"{company} Insights Report")
    c.setFillColorRGB(mr, mg, mb)
    c.setFont("Helvetica", 10)
    sub = (generated_at or "")
    if sub:
        sub = f"Generated: {sub}"
    if filters:
        sub = (sub + "   |   " if sub else "") + f"Filters: {filters}"
    c.drawString(x0 + 14, y - 52, sub[:140])
    y -= header_h + 10

    # KPI row
    kpi_items = [
        ("Total employees", str(kpi.get("total") or "")),
        ("Locations", str(kpi.get("locations") or "")),
        ("Departments", str(kpi.get("departments") or "")),
        ("Countries", str(kpi.get("countries") or "")),
        ("Cities", str(kpi.get("cities") or "")),
    ]
    box_gap = 8
    box_w = (page_w - 2 * margin - 4 * box_gap) / 5.0
    box_h = 46
    for i, (label, val) in enumerate(kpi_items):
        bx = x0 + i * (box_w + box_gap)
        _pdf_draw_card(c, bx, y - box_h, box_w, box_h, theme)
        c.setFillColorRGB(tr, tg, tb)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(bx + 10, y - 24, (val or "–")[:18])
        c.setFillColorRGB(mr, mg, mb)
        c.setFont("Helvetica", 9)
        c.drawString(bx + 10, y - 42, label)
    y -= box_h + 10

    # Charts layout
    col_gap = 12
    col_w = (page_w - 2 * margin - col_gap) / 2.0
    # 2x2 grid for landscape A4.
    # Increase height so the charts fill the page better (reduce bottom whitespace).
    card_h = 184

    def _draw_image_card(
        title: str,
        data_url: str,
        x: float,
        y_bottom: float,
        w: float,
        h: float,
        pad: float = 12,
        title_h: float = 38,
    ):
        _pdf_draw_card(c, x, y_bottom, w, h, theme)
        c.setFillColorRGB(tr, tg, tb)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x + pad, y_bottom + h - 22, title)

        img_bytes = _safe_b64_png_to_bytes(data_url)
        if not img_bytes:
            c.setFillColorRGB(mr, mg, mb)
            c.setFont("Helvetica", 9)
            c.drawString(x + pad, y_bottom + h - 44, "(Snapshot unavailable)")
            return
        try:
            img = ImageReader(io.BytesIO(img_bytes))
            img_x = x + pad
            img_y = y_bottom + pad
            img_w = w - 2 * pad
            img_h = h - title_h - pad
            c.drawImage(img, img_x, img_y, width=img_w, height=img_h, preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            c.setFillColorRGB(mr, mg, mb)
            c.setFont("Helvetica", 9)
            c.drawString(x + pad, y_bottom + h - 44, "(Failed to render image)")

    # Expect PNG data URLs from the frontend
    top_row_bottom = y - card_h
    _draw_image_card(
        "World Map (Countries)",
        str(charts.get("map") or ""),
        x=x0,
        y_bottom=top_row_bottom,
        w=col_w,
        h=card_h,
        # Make the top row feel "zoomed": tighter padding and taller image area.
        pad=8,
        title_h=30,
    )
    _draw_image_card(
        "Employees by City",
        str(charts.get("city") or ""),
        x=x0 + col_w + col_gap,
        y_bottom=top_row_bottom,
        w=col_w,
        h=card_h,
        pad=8,
        title_h=30,
    )

    # Bottom row (side-by-side)
    bottom_row_top = top_row_bottom - 8
    bottom_row_bottom = bottom_row_top - card_h
    _draw_image_card(
        "Location Distribution",
        str(charts.get("loc") or ""),
        x=x0,
        y_bottom=bottom_row_bottom,
        w=col_w,
        h=card_h,
    )
    _draw_image_card(
        "Employees by Department",
        str(charts.get("dept") or ""),
        x=x0 + col_w + col_gap,
        y_bottom=bottom_row_bottom,
        w=col_w,
        h=card_h,
    )

    c.showPage()
    c.save()
    pdf = buf.getvalue()
    buf.close()

    filename = re.sub(r"[^a-zA-Z0-9_\-]+", "_", company).strip("_") or "company"
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}_insights_report.pdf"'
    return resp


@app.route("/", methods=["GET"])
def root():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return render_template("landing_page.html")



@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return redirect(url_for("root", _anchor="login"))

    email = (request.form.get("email") or "").strip()
    access_token = (request.form.get("token") or "").strip()

    if not email or not access_token:
        flash("Please enter both email and access token.", "error")
        # If the request came from the landing page, we want to show the error there.
        # But since we use a simple redirect, we just use flash.
        return redirect(url_for("root", _anchor="login"))

    try:
        resp = graph_get_me(access_token)
    except requests.exceptions.SSLError:
        flash(
            "SSL error calling Microsoft Graph. "
            "If you're on a corporate network, set VERIFY_SSL=false "
            "or configure your corporate root CA.",
            "error",
        )
        return redirect(url_for("root", _anchor="login"))
    except Exception as e:
        app.logger.exception("Unexpected error calling Graph")
        flash(f"Unexpected error calling Graph: {e}", "error")
        return redirect(url_for("root", _anchor="login"))

    if resp.status_code == 200:
        me = resp.json() or {}
        graph_mail = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        if graph_mail == email.lower():
            session.permanent = True
            session["user"] = {
                "display_name": me.get("displayName"),
                "email": graph_mail,
            }
            session["access_token"] = access_token
            # --- New token expiry handling ---
            exp = _token_expiry_epoch(access_token)
            if exp:
                session["token_exp"] = exp
                session["token_expires_in_sec"] = max(0, exp - int(time.time()))
            else:
                session.pop("token_exp", None)
                session.pop("token_expires_in_sec", None)

            # Warm key caches in the background so navigation is instant.
            _kickoff_post_login_prefetch(access_token)
            
            # Check for return_to parameter
            return_to = request.form.get("return_to")
            if return_to and not return_to.startswith(('http://', 'https://')):
                # Simple check for local redirects only
                return redirect(return_to)
            
            return redirect(url_for("dashboard"))
        else:
            flash("Token is valid but does NOT belong to the provided email.", "error")
            return redirect(url_for("root", _anchor="login"))
    else:
        try:
            details = resp.json()
        except Exception:
            details = resp.text
        app.logger.debug(f"Graph error {resp.status_code}: {details}")
        flash("Invalid token or Microsoft Graph rejected the request.", "error")
        return redirect(url_for("root", _anchor="login"))


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    # --- Token expiry (best-effort) ---
    token_exp = session.get("token_exp")  # epoch seconds
    expires_in_sec = None
    expires_at_local = None

    try:
        if token_exp:
            now = int(time.time())
            expires_in_sec = max(0, int(token_exp) - now)
            # local time string (server local time)
            expires_at_local = datetime.datetime.fromtimestamp(int(token_exp)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        expires_in_sec = None
        expires_at_local = None

    def _fmt_duration(s: Optional[int]) -> str:
        if s is None:
            return "Unknown"
        if s <= 0:
            return "Expired"
        h = s // 3600
        m = (s % 3600) // 60
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"

    return render_template(
        "dashboard.html",
        user=session["user"],
        token_expires_at=expires_at_local,
        token_expires_in=_fmt_duration(expires_in_sec),
        prefetch_count=session.get("prefetch_count")
    )


@app.route("/employees", methods=["GET", "POST"])
def employees():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    available_fields = _employees_available_fields()
    available_field_names = [k for k, _ in available_fields]

    # Defaults
    selected_fields = ["displayName", "mail", "jobTitle", "department"]
    search = ""
    dept = ""
    job_title = ""
    company = ""
    office_location = ""
    city = ""
    # Default to 500 rows; max_items=0 means "no cap" (fetch all)
    max_items = 500

    if request.method == "POST":
        selected_fields = request.form.getlist("fields") or selected_fields
        selected_fields = [f for f in selected_fields if f in available_field_names]
        search = (request.form.get("search") or "").strip()
        dept = (request.form.get("department") or "").strip()
        job_title = (request.form.get("job_title") or "").strip()
        company = (request.form.get("company") or "").strip()
        office_location = (request.form.get("office_location") or "").strip()
        city = (request.form.get("city") or "").strip()
        raw_max = (request.form.get("max_items") or "").strip()
        if raw_max:
            try:
                max_items = int(raw_max)
            except ValueError:
                max_items = 500
            # Allow 0 for "all"; otherwise clamp to a reasonable ceiling.
            if max_items != 0:
                max_items = max(1, min(max_items, 50000))

    headers = {"Authorization": f"Bearer {session['access_token']}"}
    rows: List[Dict[str, Any]] = []
    error: Optional[str] = None

    # Fast path: if post-login prefetch warmed a default dataset, reuse it.
    try:
        cache = getattr(app, "_employees_cache", None)
        if cache and cache.get("rows"):
            now = time.time()
            ttl_sec = 300
            key = (tuple(selected_fields), search, dept, job_title, company, office_location, city, max_items)
            if cache.get("key") == key and (now - float(cache.get("ts") or 0) < ttl_sec):
                rows = cache.get("rows") or []
            else:
                rows = _employees_fetch(
                    headers,
                    selected_fields,
                    search=search,
                    dept=dept,
                    job_title=job_title,
                    company=company,
                    office_location=office_location,
                    city=city,
                    max_items=max_items,
                )
                # Save result for the next request.
                cache["ts"] = now
                cache["key"] = key
                cache["rows"] = rows
        else:
            rows = _employees_fetch(
                headers,
                selected_fields,
                search=search,
                dept=dept,
                job_title=job_title,
                company=company,
                office_location=office_location,
                city=city,
                max_items=max_items,
            )
    except Exception as e:
        error = str(e)

    return render_template(
        "employees.html",
        user=session.get("user"),
        available_fields=available_fields,
        selected_fields=selected_fields,
        search=search,
        department=dept,
        job_title=job_title,
        company=company,
        office_location=office_location,
        city=city,
        max_items=max_items,
        rows=rows,
        error=error,
    )


@app.route("/employees/export_csv", methods=["POST"])
def employees_export_csv():
    if "access_token" not in session:
        return redirect(url_for("root", _anchor="login"))

    available_fields = _employees_available_fields()
    available_field_names = [k for k, _ in available_fields]

    selected_fields = request.form.getlist("fields") or ["displayName", "mail", "jobTitle", "department"]
    selected_fields = [f for f in selected_fields if f in available_field_names]
    search = (request.form.get("search") or "").strip()
    dept = (request.form.get("department") or "").strip()
    job_title = (request.form.get("job_title") or "").strip()
    company = (request.form.get("company") or "").strip()
    office_location = (request.form.get("office_location") or "").strip()
    city = (request.form.get("city") or "").strip()
    raw_max = (request.form.get("max_items") or "").strip()
    if raw_max:
        try:
            max_items = int(raw_max)
        except ValueError:
            max_items = 0
    else:
        # For exports, default is still "all" unless user picks a specific number.
        max_items = 0
    # Allow 0 for "all"; otherwise clamp to a reasonable ceiling.
    if max_items != 0:
        max_items = max(1, min(max_items, 50000))

    headers = {"Authorization": f"Bearer {session['access_token']}"}
    rows = _employees_fetch(
        headers,
        selected_fields,
        search=search,
        dept=dept,
        job_title=job_title,
        company=company,
        office_location=office_location,
        city=city,
        max_items=max_items,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(selected_fields)
    for r in rows:
        writer.writerow([r.get(f, "") if r.get(f, "") is not None else "" for f in selected_fields])

    csv_bytes = output.getvalue().encode("utf-8")
    resp = make_response(csv_bytes)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=employees.csv"
    return resp


@app.route("/organization", methods=["GET"])
def organization_redirect():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))
    return redirect(url_for("hierarchy"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return render_template("landing_page.html", user=None)


@app.route("/api/search_suggestions")
def search_suggestions():
    """Returns a list of users matching the query for inline suggestions."""
    if not session.get("user"):
        return {"users": []}, 401
    
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return {"users": []}

    token = session.get("access_token")
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        # Search by display name or mail
        search_filter = f"startswith(displayName,'{q}') or startswith(mail,'{q}') or startswith(userPrincipalName,'{q}')"
        url = f"https://graph.microsoft.com/v1.0/users?$filter={urllib.parse.quote(search_filter)}&$select=id,displayName,mail,userPrincipalName,jobTitle&$top=6"
        res = _graph_get(url, headers)
        if res.status_code == 200:
            users = res.json().get("value", [])
            return {"users": users}
        return {"users": []}
    except Exception:
        return {"users": []}


@app.route("/profile/photo")
def profile_photo_user():
    """
    Return profile photo for a specified user (id / userPrincipalName / mail) via ?user=.
    If no user query param is provided, falls back to /me (current token owner).
    """
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    token = session.get("access_token")
    if not token:
        return _transparent_png()

    user_q = (request.args.get("user") or "").strip()
    try:
        headers = {"Authorization": f"Bearer {token}"}
        if user_q:
            target = urllib.parse.quote(user_q, safe='')
            photo_url = f"https://graph.microsoft.com/v1.0/users/{target}/photo/$value"
        else:
            photo_url = "https://graph.microsoft.com/v1.0/me/photo/$value"

        r = requests.get(photo_url, headers=headers, verify=VERIFY_SSL, stream=True)
        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "image/jpeg")
            return Response(r.content, status=200, mimetype=content_type)

        # no photo -> render initials for the requested user if possible
        if r.status_code == 404:
            display_name = None
            if user_q:
                try:
                    u = _fetch_user(user_q, headers, ["displayName", "mail", "userPrincipalName"])
                    display_name = u.get("displayName") if u else user_q
                except Exception:
                    display_name = user_q
            else:
                display_name = session["user"].get("display_name") or session["user"].get("email")
            return _initials_svg(display_name or "?")

        app.logger.debug(f"Photo fetch failed ({photo_url}): {r.status_code} {r.text}")
        return _transparent_png()
    except Exception:
        app.logger.exception("Profile photo fetch error")
        return _transparent_png()


def _transparent_png():
    """Return a 1x1 transparent PNG."""
    import base64

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQAB"
        "DQottAAAAABJRU5ErkJggg=="
    )
    data = base64.b64decode(png_b64)
    return Response(data, status=200, mimetype="image/png")


def _initials_svg(display_text: str):
    """Render a simple SVG avatar with user initials as a friendly fallback."""
    initials = "".join([part[0] for part in display_text.split()[:2]]).upper() or "?"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">
  <rect width="100%" height="100%" fill="#1f2937"/>
  <text x="50%" y="55%" font-size="40" text-anchor="middle" fill="#e5e7eb" font-family="Arial, sans-serif">{initials}</text>
</svg>"""
    return Response(svg, status=200, mimetype="image/svg+xml")


def _graph_get(url: str, headers: Dict[str, str]):
    """Simple wrapper for Graph GET with SSL handling."""
    return requests.get(url, headers=headers, verify=VERIFY_SSL)


def _graph_get_paged(url: str, headers: Dict[str, str], max_items: int = 2000) -> List[Dict[str, Any]]:
    """Fetch a paged Graph collection (value + @odata.nextLink)."""
    items: List[Dict[str, Any]] = []
    next_url = url
    # max_items=0 means "no cap" (fetch all), but keep a generous internal ceiling.
    ceiling = 50000 if max_items == 0 else max_items
    while next_url and len(items) < ceiling:
        r = _graph_get(next_url, headers)
        if r.status_code != 200:
            break
        body = r.json() or {}
        items.extend(body.get("value", []) or [])
        next_url = body.get("@odata.nextLink")
    return items if max_items == 0 else items[:max_items]


def _build_users_filter_q(
    search: str,
    dept: str,
    job_title: str = "",
    company: str = "",
    office_location: str = "",
    city: str = "",
) -> str:
    """Create a safe (simple) $filter expression for /users. Returns '' if no filter.

    Notes:
    - Graph string literals use single quotes.
    - We escape single quotes by doubling them.
    - We keep the filter conservative to avoid syntax errors.
    """
    parts: List[str] = []
    if dept:
        d = dept.replace("'", "''")
        parts.append(f"department eq '{d}'")
    if job_title:
        jt = job_title.replace("'", "''")
        parts.append(f"jobTitle eq '{jt}'")
    if company:
        cn = company.replace("'", "''")
        parts.append(f"companyName eq '{cn}'")
    if office_location:
        ol = office_location.replace("'", "''")
        parts.append(f"officeLocation eq '{ol}'")
    if city:
        c = city.replace("'", "''")
        parts.append(f"city eq '{c}'")
    if search:
        s = search.replace("'", "''")
        # Simple contains filter across common fields.
        parts.append(
            "(" +
            f"startswith(displayName,'{s}') or "
            f"startswith(mail,'{s}') or "
            f"startswith(userPrincipalName,'{s}')" +
            ")"
        )
    return " and ".join(parts)


def _employees_available_fields() -> List[tuple]:
    return [
        ("displayName", "Full Name"),
        ("mail", "Email"),
        ("userPrincipalName", "UPN"),
        ("jobTitle", "Designation"),
        ("department", "Department"),
        ("companyName", "Company"),
        ("officeLocation", "Office Location"),
        ("mobilePhone", "Mobile"),
        ("city", "City"),
        ("country", "Country"),
        ("employeeType", "Employment Type"),
        ("employeeId", "Employee ID"),
    ]


def _employees_fetch(
    headers: Dict[str, str],
    selected_fields: List[str],
    search: str = "",
    dept: str = "",
    job_title: str = "",
    company: str = "",
    office_location: str = "",
    city: str = "",
    max_items: int = 1000,
) -> List[Dict[str, Any]]:
    # Graph requires ConsistencyLevel for some advanced queries; we keep it simple.
    select_fields = list(dict.fromkeys(["id"] + (selected_fields or [])))
    select_q = ",".join(select_fields)

    base = "https://graph.microsoft.com/v1.0/users"
    params: List[str] = [f"$top=999", f"$select={urllib.parse.quote(select_q)}"]

    filt = _build_users_filter_q(
        search=search,
        dept=dept,
        job_title=job_title,
        company=company,
        office_location=office_location,
        city=city,
    )
    if filt:
        params.append("$filter=" + urllib.parse.quote(filt))

    url = base + "?" + "&".join(params)
    return _graph_get_paged(url, headers, max_items=max_items)


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def _graph_headers_from_token(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _insights_fetch_company_name(headers: Dict[str, str]) -> Optional[str]:
    """Best-effort fetch of tenant/company name from Graph.

    Uses GET /organization (typically returns a single organization with displayName).
    Returns None if unavailable.
    """
    try:
        url = "https://graph.microsoft.com/v1.0/organization?$select=displayName"
        r = _graph_get(url, headers)
        if r.status_code != 200:
            return None
        body = r.json() or {}
        items = body.get("value") or []
        if items and isinstance(items, list):
            name = (items[0].get("displayName") or "").strip()
            return name or None
    except Exception:
        return None
    return None


def _graph_get_paged_raise(url: str, headers: Dict[str, str], max_items: int = 5000) -> List[Dict[str, Any]]:
    """Paged Graph GET that raises helpful errors (for simulator insights)."""
    items: List[Dict[str, Any]] = []
    next_url = url
    # max_items=0 means "no cap" (fetch all), but keep a generous internal ceiling.
    ceiling = 50000 if max_items == 0 else max_items
    while next_url and len(items) < ceiling:
        r = _graph_get(next_url, headers)
        if r.status_code != 200:
            raise RuntimeError(f"Graph error {r.status_code}: {_safe_json(r)}")
        body = r.json() or {}
        items.extend(body.get("value", []) or [])
        next_url = body.get("@odata.nextLink")
    return items if max_items == 0 else items[:max_items]


def _org_sim_fetch_users_with_manager(headers: Dict[str, str], max_items: int = 0) -> List[Dict[str, Any]]:
    # Get a working set of properties + manager navigation property.
    # Graph returns manager as directoryObject when expanded.
    select = "id,displayName,mail,userPrincipalName,jobTitle,department"
    url = (
        "https://graph.microsoft.com/v1.0/users"
        f"?$select={urllib.parse.quote(select)}"
        "&$expand=manager($select=id,displayName,mail,userPrincipalName)"
        "&$top=999"
    )
    h = dict(headers)
    h.setdefault("Prefer", "odata.maxpagesize=999")
    # max_items=0 means "no cap" (fetch all). We'll still guard with a generous internal ceiling.
    ceiling = 50000 if max_items == 0 else max_items
    return _graph_get_paged_raise(url, h, max_items=ceiling)


def _org_graph_from_users(users: List[Dict[str, Any]]):
    """Build adjacency and reverse adjacency from users + expanded manager."""
    nodes: Dict[str, Dict[str, Any]] = {}
    manager_of: Dict[str, Optional[str]] = {}
    reports: Dict[str, List[str]] = defaultdict(list)

    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        nodes[uid] = {
            "id": uid,
            "displayName": u.get("displayName"),
            "mail": u.get("mail"),
            "userPrincipalName": u.get("userPrincipalName"),
            "jobTitle": u.get("jobTitle"),
            "department": u.get("department"),
        }
        mgr = u.get("manager")
        mid = None
        if isinstance(mgr, dict):
            mid = mgr.get("id")
        manager_of[uid] = mid
        if mid:
            reports[mid].append(uid)

    # Add any managers outside the fetched set as stub nodes (keeps edges consistent)
    for uid, mid in list(manager_of.items()):
        if mid and mid not in nodes:
            nodes[mid] = {"id": mid, "displayName": "(Manager)", "mail": None, "userPrincipalName": None, "jobTitle": None, "department": None}

    return nodes, manager_of, reports


def _org_sim_analyze(nodes: Dict[str, Dict[str, Any]], manager_of: Dict[str, Optional[str]], reports: Dict[str, List[str]], removed_ids: Set[str]):
    # Remaining nodes
    remaining = {nid for nid in nodes.keys() if nid not in removed_ids}

    # Orphans: have manager but manager removed/missing
    orphans: List[str] = []
    for nid in remaining:
        mid = manager_of.get(nid)
        if mid and mid not in remaining:
            orphans.append(nid)

    # Isolated: no manager in remaining and no direct reports in remaining
    isolated: List[str] = []
    for nid in remaining:
        mid = manager_of.get(nid)
        has_mgr = bool(mid and mid in remaining)
        has_reports = any(r in remaining for r in (reports.get(nid) or []))
        if (not has_mgr) and (not has_reports):
            isolated.append(nid)

    # Disconnected components (treat undirected edges manager<->report)
    adj: Dict[str, Set[str]] = {nid: set() for nid in remaining}
    for nid in remaining:
        mid = manager_of.get(nid)
        if mid and mid in remaining:
            adj[nid].add(mid)
            adj[mid].add(nid)
        for r in (reports.get(nid) or []):
            if r in remaining:
                adj[nid].add(r)
                adj[r].add(nid)

    comps: List[List[str]] = []
    seen: Set[str] = set()
    for nid in remaining:
        if nid in seen:
            continue
        stack = [nid]
        comp: List[str] = []
        seen.add(nid)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj.get(x, set()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        comps.append(comp)
    comps.sort(key=len, reverse=True)

    # "Multiple team membership" (approximation): user has both a manager and at least one direct report.
    multi_team: List[str] = []
    for nid in remaining:
        mid = manager_of.get(nid)
        has_mgr = bool(mid and mid in remaining)
        has_reports = any(r in remaining for r in (reports.get(nid) or []))
        if has_mgr and has_reports:
            multi_team.append(nid)

    insights = {
        "total_nodes": len(nodes),
        "remaining_nodes": len(remaining),
        "removed": len(removed_ids),
        "orphans": len(orphans),
        "isolated": len(isolated),
        "components": len(comps),
        "largest_component": len(comps[0]) if comps else 0,
        "multi_team_membership": len(multi_team),
    }

    def slim(nid: str) -> Dict[str, Any]:
        u = nodes.get(nid) or {"id": nid}
        return {
            "id": nid,
            "displayName": u.get("displayName"),
            "mail": u.get("mail") or u.get("userPrincipalName"),
            "jobTitle": u.get("jobTitle"),
            "department": u.get("department"),
        }

    # Enhanced Hierarchy Comparison: Build tree structures
    # We build a tree for components that contain the removed nodes or their immediate reports
    affected_roots = set()
    for rid in removed_ids:
        mid = manager_of.get(rid)
        if mid: affected_roots.add(mid)
        affected_roots.add(rid)
        for rep in (reports.get(rid) or []):
            affected_roots.add(rep)

    def build_tree(nid, current_remaining, depth=0, visited=None):
        if visited is None: visited = set()
        if nid in visited or depth > 4: return None
        visited.add(nid)
        
        u = slim(nid)
        children = []
        for rid in (reports.get(nid) or []):
            child = build_tree(rid, current_remaining, depth + 1, visited)
            if child:
                children.append(child)
        u["children"] = children
        return u

    # Find the top-most managers for the affected areas
    roots = set()
    for nid in affected_roots:
        curr = nid
        # Go up at most 2 levels to find a stable root
        for _ in range(2):
            parent = manager_of.get(curr)
            if parent and parent in nodes:
                curr = parent
            else:
                break
        roots.add(curr)

    # Filtered Hierarchy: Only show the removed users and their direct reports
    original_trees = []
    
    for rid in removed_ids:
        u = nodes.get(rid)
        if not u: continue
        
        # Build a 1-level tree: the removed manager + their direct reports
        root = slim(rid)
        children = []
        for d_report_id in (reports.get(rid) or []):
            dr = slim(d_report_id)
            dr["children"] = [] # No grandchild traversal as requested
            children.append(dr)
        
        root["children"] = children
        original_trees.append(root)

    # Simulated Trees use the same structure, but the frontend will ghost the root
    simulated_trees = copy.deepcopy(original_trees)

    # Calculate Replacement Suggestions for directly impacted reports
    replacement_suggestions = []
    for rid in removed_ids:
        direct_reps = reports.get(rid) or []
        if direct_reps:
            # Simple heuristic: Peer with most reports or longest tenure? 
            # For now, let's pick the "most senior" direct report or a peer
            manager_id = manager_of.get(rid)
            candidates = []
            
            # 1. Peers (sharing same manager)
            if manager_id:
                peers = [p for p in reports.get(manager_id, []) if p != rid and p not in removed_ids]
                for p in peers:
                    c = slim(p)
                    c["type"] = "Peer Transition"
                    c["score"] = len(reports.get(p) or []) + 5
                    candidates.append(c)
            
            # 2. Direct Reports (Step up)
            for dr in direct_reps:
                if dr not in removed_ids:
                    c = slim(dr)
                    c["type"] = "Direct Step-up"
                    c["score"] = len(reports.get(dr) or [])
                    candidates.append(c)
            
            candidates.sort(key=lambda x: x["score"], reverse=True)
            if candidates:
                replacement_suggestions.append({
                    "manager": slim(rid),
                    "candidates": candidates[:3]
                })

    return {
        "insights": insights,
        "orphans": [slim(x) for x in sorted(orphans)],
        "isolated": [slim(x) for x in sorted(isolated)],
        "multi_team": [slim(x) for x in sorted(multi_team)],
        "components": [
            {
                "size": len(c),
                "sample": [slim(x) for x in c[:8]],
            }
            for c in comps[:10]
        ],
        "ins_original_tree": original_trees,
        "ins_simulated_tree": simulated_trees,
        "replacements": replacement_suggestions
    }


@app.route("/department-search", methods=["GET"])
def department_search():
    """Department suggestion endpoint for typeahead UIs.

    Uses Microsoft Graph users list to collect distinct department names.
    """
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    headers = _graph_headers_from_token(token)
    base = "https://graph.microsoft.com/v1.0/users"
    select = "department"
    starts = q.replace("'", "''")
    # Filter to departments that start with the query and are not null/empty.
    filt = f"department ne null and startswith(department,'{starts}')"
    url = (
        f"{base}?$top=999"
        f"&$select={urllib.parse.quote(select)}"
        f"&$filter={urllib.parse.quote(filt)}"
    )

    try:
        items = _graph_get_paged_raise(url, headers)
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

    seen = set()
    out = []
    for u in items:
        d = (u.get("department") or "").strip()
        if not d:
            continue
        k = d.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
        if len(out) >= limit:
            break

    out.sort(key=lambda s: s.casefold())
    return {"ok": True, "items": out}, 200


def _distinct_user_field_suggestions(token: str, field: str, q: str, limit: int = 20):
    """Return distinct values for a given /users string field starting with query.

    Uses Graph $filter with startswith(field,'q') for typeahead suggestions.
    """
    headers = _graph_headers_from_token(token)
    base = "https://graph.microsoft.com/v1.0/users"

    # Graph: ensure field is safe (allowlist)
    allow = {"department", "jobTitle", "companyName", "officeLocation", "city"}
    if field not in allow:
        raise ValueError("Unsupported field")

    starts = q.replace("'", "''")
    select = field
    # Filter to values that start with the query and are not null.
    filt = f"{field} ne null and startswith({field},'{starts}')"
    url = (
        f"{base}?$top=999"
        f"&$select={urllib.parse.quote(select)}"
        f"&$filter={urllib.parse.quote(filt)}"
    )

    items = _graph_get_paged_raise(url, headers)
    seen = set()
    out = []
    for u in items:
        v = (u.get(field) or "").strip()
        if not v:
            continue
        k = v.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= limit:
            break
    out.sort(key=lambda s: s.casefold())
    return out


@app.route("/job-title-search", methods=["GET"])
def job_title_search():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        out = _distinct_user_field_suggestions(token, "jobTitle", q, limit=limit)
        return {"ok": True, "items": out}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/company-search", methods=["GET"])
def company_search():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        out = _distinct_user_field_suggestions(token, "companyName", q, limit=limit)
        return {"ok": True, "items": out}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/office-location-search", methods=["GET"])
def office_location_search():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        out = _distinct_user_field_suggestions(token, "officeLocation", q, limit=limit)
        return {"ok": True, "items": out}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/city-search", methods=["GET"])
def city_search():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        out = _distinct_user_field_suggestions(token, "city", q, limit=limit)
        return {"ok": True, "items": out}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/user-search", methods=["GET"])
def user_search():
    """Reusable user suggestion endpoint for search dropdowns.

    Returns small list of users for typeahead UIs.
    """
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    q = (request.args.get("q") or "").strip()
    if not q:
        return {"ok": True, "items": []}, 200

    try:
        limit = int(request.args.get("limit") or 25)
    except ValueError:
        limit = 25
    limit = max(1, min(limit, 50))

    headers = _graph_headers_from_token(token)

    select = "id,displayName,mail,userPrincipalName,jobTitle"
    base = "https://graph.microsoft.com/v1.0/users"
    search_q = q.replace('"', "\\\"")
    url = (
        f"{base}?$top={limit}"
        f"&$select={urllib.parse.quote(select)}"
        f"&$search={urllib.parse.quote('"' + search_q + '"')}"
    )
    h2 = dict(headers)
    h2["ConsistencyLevel"] = "eventual"

    r = _graph_get(url, h2)
    if r.status_code != 200:
        raw = q.replace("'", "''")
        filt = (
            f"startswith(displayName,'{raw}') or "
            f"startswith(mail,'{raw}') or "
            f"startswith(userPrincipalName,'{raw}')"
        )
        url2 = (
            f"{base}?$top={limit}"
            f"&$select={urllib.parse.quote(select)}"
            f"&$filter={urllib.parse.quote(filt)}"
        )
        r2 = _graph_get(url2, headers)
        if r2.status_code != 200:
            return {"ok": False, "error": f"Graph error {r2.status_code}: {_safe_json(r2)}"}, 500
        body = r2.json() or {}
    else:
        body = r.json() or {}

    items = body.get("value", []) or []
    out = []
    for u in items:
        out.append(
            {
                "id": u.get("id"),
                "displayName": u.get("displayName"),
                "mail": u.get("mail"),
                "userPrincipalName": u.get("userPrincipalName"),
                "jobTitle": u.get("jobTitle"),
            }
        )
    return {"ok": True, "items": out}, 200


def _fetch_user(identifier: str, headers: Dict[str, str], select: List[str] = None) -> Dict:
    """Fetch a user by id or userPrincipalName/email. Always request 'id'. Falls back to filter search."""
    sel_list = list(select or [])
    if "id" not in sel_list:
        sel_list.append("id")
    sel = ""
    if sel_list:
        sel = "?$select=" + ",".join(sel_list)

    # Try direct GET by identifier (id or userPrincipalName)
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(identifier)}{sel}"
    r = _graph_get(url, headers)
    if r.status_code == 200:
        return r.json()

    # Fallback: search by mail or userPrincipalName using $filter
    raw = identifier.replace("'", "''")
    filt = f"mail eq '{raw}' or userPrincipalName eq '{raw}'"
    url2 = "https://graph.microsoft.com/v1.0/users?$filter=" + urllib.parse.quote(filt) + "&$select=" + ",".join(sel_list)
    r2 = _graph_get(url2, headers)
    if r2.status_code == 200:
        body = r2.json() or {}
        items = body.get("value", [])
        if items:
            return items[0]
    return None


def _fetch_manager(user_id: str, headers: Dict[str, str]) -> Dict:
    """Return manager object or None (manager endpoint often returns a directoryObject)."""
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(user_id)}/manager"
    r = _graph_get(url, headers)
    if r.status_code != 200:
        return None
    mgr = r.json()
    return mgr


def _fetch_direct_reports(user_id: str, headers: Dict[str, str], select: List[str] = None) -> List[Dict]:
    sel = ""
    if select:
        sel = "?$select=" + ",".join(select)
    url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(user_id)}/directReports{sel}"
    r = _graph_get(url, headers)
    if r.status_code != 200:
        return []
    body = r.json() or {}
    return body.get("value", [])


def _build_upward_chain(start_user: Dict, headers: Dict[str, str], select: List[str]) -> List[Dict]:
    chain: List[Dict] = []
    visited: Set[str] = set()

    current = start_user
    if not current:
        return chain

    while current:
        uid = current.get("id") or current.get("userPrincipalName") or current.get("mail")
        if not uid or uid in visited:
            break
        visited.add(uid)

        out_node: Dict[str, Any] = {}
        for k in (select or []):
            if k in current and current.get(k) is not None:
                out_node[k] = current.get(k)
        out_node.setdefault("id", current.get("id") or current.get("userPrincipalName") or current.get("mail"))
        out_node.setdefault("displayName", current.get("displayName"))
        out_node.setdefault("mail", current.get("mail"))
        out_node.setdefault("userPrincipalName", current.get("userPrincipalName"))

        chain.append(out_node)

        mgr_obj = _fetch_manager(uid, headers)
        if not mgr_obj:
            break
        mgr_id = mgr_obj.get("id") or mgr_obj.get("userPrincipalName") or mgr_obj.get("mail")
        if not mgr_id:
            break

        # Fetch full manager profile (so we have the selected fields) and continue walking upward.
        mgr_full = _fetch_user(mgr_id, headers, select)
        current = mgr_full if mgr_full else mgr_obj

    chain.reverse()
    return chain



def _build_downward_tree(user: Dict, headers: Dict[str, str], select: List[str], visited: Set[str], max_depth: int = 99) -> Dict:
    uid = user.get("id") or user.get("userPrincipalName") or user.get("mail")
    node = {k: user.get(k) for k in select + (["id"] if "id" not in select else [])}
    node["children"] = []
    if not uid or uid in visited or max_depth <= 0:
        return node
    visited.add(uid)
    reports = _fetch_direct_reports(uid, headers, select)
    for r in reports:
        child_full = r
        if not child_full.get("id"):
            candidate = r.get("userPrincipalName") or r.get("mail")
            if candidate:
                child_full = _fetch_user(candidate, headers, select) or r
        child_node = _build_downward_tree(child_full, headers, select, visited, max_depth - 1)
        node["children"].append(child_node)
    return node


@app.route("/hierarchy", methods=["GET", "POST"])
def hierarchy():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    available_fields = [
        ("displayName", "Full Name"),
        ("mail", "Email"),
        ("employeeType", "Employment Type"),
        ("companyName", "Company"),
        ("department", "Department"),
        ("jobTitle", "Designation"),
        ("officeLocation", "OfficeLocation"),
        ("mobilePhone", "Contact"),
        ("city", "City"),
        ("country", "Country"),
        ("userPrincipalName", "UPN"),
        #("businessPhones", "Business phones"),
        ("employeeId", "Employee ID"),
        ("givenName", "Given name"),
        ("surname", "Surname"),
        ("streetAddress", "Office address"),
        ("postalCode", "Postal code"),
    ]

    if request.method == "GET":
        # Teams-style: default to the logged-in user and show their upward chain.
        # You can override via query params: ?email=someone@contoso.com&direction=downward
        q_email = (request.args.get("email") or "").strip()
        q_direction = (request.args.get("direction") or "").strip().lower()
        q_fields = request.args.getlist("fields")
        q_max_depth = request.args.get("max_depth")

        # Only auto-render hierarchy if email is present (either query or session user).
        email = q_email or (session.get("user", {}) or {}).get("email")
        direction = q_direction or "upward"
        selected_fields = q_fields or ["displayName", "mail", "jobTitle"]
        try:
            max_depth = int(q_max_depth) if q_max_depth else 99
        except ValueError:
            max_depth = 99

        token = session.get("access_token")
        if email and token:
            headers = {"Authorization": f"Bearer {token}"}
            try:
                target = _fetch_user(email, headers, selected_fields)
                if target:
                    if direction == "downward":
                        tree = _build_downward_tree(target, headers, selected_fields, visited=set(), max_depth=max_depth)
                        return render_template(
                            "hierarchy.html",
                            hierarchy=tree,
                            direction=direction,
                            selected_fields=selected_fields,
                            available_fields=available_fields,
                        )
                    chain = _build_upward_chain(target, headers, selected_fields)
                    return render_template(
                        "hierarchy.html",
                        hierarchy={"chain": chain},
                        direction=direction,
                        selected_fields=selected_fields,
                        available_fields=available_fields,
                    )
            except Exception:
                # Fall through to empty state; errors are shown on POST flows.
                pass

        return render_template(
            "hierarchy.html",
            available_fields=available_fields,
            direction=direction,
            selected_fields=selected_fields,
        )

    email = (request.form.get("email") or "").strip()
    direction = (request.form.get("direction") or "upward").strip().lower()
    selected_fields = request.form.getlist("fields") or ["displayName", "mail", "jobTitle"]
    max_depth = int(request.form.get("max_depth") or 99)

    token = session.get("access_token")
    if not email or not token:
        flash("Please enter an email and be logged in.", "error")
        return render_template("hierarchy.html", available_fields=available_fields)

    headers = {"Authorization": f"Bearer {token}"}

    try:
        target = _fetch_user(email, headers, selected_fields)
        if not target:
            flash("User not found in Graph.", "error")
            return render_template("hierarchy.html", available_fields=available_fields)

        if direction == "downward":
            # OPTION 3: Lazy Loading. Fetch ONLY the user and their immediate reports.
            uid = target.get("id") or target.get("userPrincipalName") or target.get("mail")
            root_user = _slim_person_with_check(target, headers)
            
            # Fetch immediate reports
            dr_data = _fetch_direct_reports(uid, headers, selected_fields)
            root_user["children"] = []
            for r in (dr_data or []):
                root_user["children"].append(_slim_person_with_check(r, headers))
            
            # Sort reports
            root_user["children"].sort(key=lambda x: (x.get("displayName") or x.get("mail") or "").lower())
            
            return render_template(
                "hierarchy.html",
                hierarchy=root_user,
                direction=direction,
                selected_fields=selected_fields,
                available_fields=available_fields,
            )

        chain = _build_upward_chain(target, headers, selected_fields)
        return render_template(
            "hierarchy.html",
            hierarchy={"chain": chain},
            direction=direction,
            selected_fields=selected_fields,
            available_fields=available_fields,
        )

    except Exception as e:
        app.logger.exception("Error fetching hierarchy from Graph")
        flash(f"Error fetching hierarchy: {e}", "error")
        return render_template("hierarchy.html", available_fields=available_fields)


def _collect_subtree_nodes(root: Dict) -> List[Dict]:
    nodes: List[Dict] = []
    stack = [root]
    visited = set()
    while stack:
        n = stack.pop()
        uid = n.get("id") or n.get("userPrincipalName") or n.get("mail")
        if not uid or uid in visited:
            continue
        visited.add(uid)
        nodes.append(n)
        for c in n.get("children", []) or []:
            stack.append(c)
    return nodes


@app.route("/hierarchy/export_csv", methods=["POST"])
def hierarchy_export_csv():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    email = (request.form.get("email") or "").strip()
    direction = (request.form.get("direction") or "upward").strip().lower()
    selected_fields = request.form.getlist("fields") or ["displayName", "mail", "jobTitle"]

    token = session.get("access_token")
    if not email or not token:
        flash("Please enter an email and be logged in.", "error")
        return redirect(url_for("hierarchy"))

    headers = {"Authorization": f"Bearer {token}"}
    target = _fetch_user(email, headers, selected_fields)
    if not target:
        flash("User not found in Graph.", "error")
        return redirect(url_for("hierarchy"))

    rows: List[Dict] = []
    if direction == "downward":
        tree = _build_downward_tree(target, headers, selected_fields, visited=set(), max_depth=999)
        nodes = _collect_subtree_nodes(tree)
        rows = nodes
    else:
        chain = _build_upward_chain(target, headers, selected_fields)
        rows = chain or []

    available_fields = [
        ("displayName", "Full Name"),
        ("mail", "Email"),
        ("employeeType", "Employment Type"),
        ("companyName", "Company"),
        ("department", "Department"),
        ("jobTitle", "Designation"),
        ("officeLocation", "OfficeLocation"),
        ("mobilePhone", "Contact"),
        ("city", "City"),
        ("country", "Country"),
        ("userPrincipalName", "UPN"),
        #("businessPhones", "Business phones"),
        ("employeeId", "Employee ID"),
        ("givenName", "Given name"),
        ("surname", "Surname"),
        ("streetAddress", "Office address"),
        ("postalCode", "Postal code"),
    ]
    label_map = {k: v for k, v in available_fields}

    si = io.StringIO()
    writer = csv.writer(si)
    header_labels = [label_map.get(f, f) for f in selected_fields]
    writer.writerow(header_labels)
    for r in rows:
        row = []
        for f in selected_fields:
            val = r.get(f, "")
            if isinstance(val, (dict, list)):
                try:
                    val = json.dumps(val, ensure_ascii=False)
                except Exception:
                    val = str(val)
            row.append(val if val is not None else "")
        writer.writerow(row)
    csv_data = si.getvalue()
    si.close()

    resp = make_response(csv_data)
    resp.headers["Content-Disposition"] = f"attachment; filename=hierarchy_{direction}_{email.replace('@','_')}.csv"
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    return resp


# --- New Graph-backed profile view (fetch data from Teams/Graph) ---
def _heuristic_extract_skills(text: str, limit: int = 8):
    if not text:
        return []
    words = re.findall(r"[A-Za-z+#\.\-]{3,}", text)
    freq: Dict[str, int] = {}
    for w in words:
        k = w.strip().lower()
        if len(k) < 2:
            continue
        freq[k] = freq.get(k, 0) + 1
    items = sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:limit]
    total = sum(v for _, v in items) or 1
    return [{"name": k.title(), "weight": round(v / total * 100, 1)} for k, v in items]


def _summarize_skills_fallback(skills_list: List[Dict[str, Any]], headline: str = "", summary_text: str = "") -> str:
    top = [s["name"] for s in skills_list[:6]]
    return f"Top skills: {', '.join(top) if top else 'None detected'}. {headline[:160]}{'...' if len(headline) > 160 else ''}"


def _pick_email_from_user_obj(u: Dict[str, Any]) -> str:
    """Best-effort mail/upn pick for linking."""
    if not u:
        return ""
    return (u.get("mail") or u.get("userPrincipalName") or u.get("id") or "").strip()


def _slim_person(u: Dict[str, Any]) -> Dict[str, Any]:
    """Return all fields from the Graph User object to preserve custom selected fields."""
    if not u:
        return {}
    # Return a copy of the dictionary instead of a hardcoded subset
    return dict(u)

def _fetch_reports_count(uid: str, headers: Dict[str, str]) -> int:
    """Checks the number of direct reports efficiently."""
    try:
        url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(uid or '')}/directReports/$count"
        # We need ConsistencyLevel: eventual for $count on some Graph relationships
        headers_with_count = headers.copy()
        headers_with_count["ConsistencyLevel"] = "eventual"
        
        url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(uid or '')}/directReports?$count=true&$top=1&$select=id"
        resp = requests.get(url, headers=headers_with_count, verify=VERIFY_SSL)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("@odata.count", 0)
    except:
        pass
    return 0

def _slim_person_with_check(p: Dict[str, Any], headers: Dict[str, str], requested_fields: List[str] = None) -> Dict[str, Any]:
    """Slim person plus a real check for reports, ensuring all requested fields are preserved."""
    uid = p.get("id") or p.get("userPrincipalName") or p.get("mail")
    count = _fetch_reports_count(uid, headers)
    
    # Start with base fields from _slim_person
    res = _slim_person(p)
    
    # Manually add any other fields requested by the user from the original graph response
    if requested_fields:
        for f in requested_fields:
            if f not in res:
                res[f] = p.get(f)
                
    res["has_reports"] = count > 0
    res["reports_count"] = count
    return res


@app.route("/hierarchy/fetch_reports")
def hierarchy_fetch_reports():
    """
    Lazy-load direct reports for any user via AJAX.
    Used by the hierarchy view to expand nodes without full page reload.
    """
    if not session.get("user"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    token = session.get("access_token")
    if not token:
        return jsonify({"ok": False, "error": "No token"}), 401

    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "error": "No user ID provided"}), 400

    headers = {"Authorization": f"Bearer {token}"}
    
    # We fetch a few extra fields for the hierarchy cards
    selected_fields = request.args.getlist("fields")
    base_fields = ["displayName", "mail", "jobTitle", "department", "userPrincipalName", "id"]
    
    # Merge selected fields with mandatory base fields
    fetch_fields = list(set(base_fields + selected_fields))
    
    try:
        dr_data = _fetch_direct_reports(uid, headers, fetch_fields)
        reports = []
        for r in (dr_data or []):
            reports.append(_slim_person_with_check(r, headers, selected_fields))
        
        # Sort by name
        reports.sort(key=lambda x: (x.get("displayName") or x.get("mail") or "").lower())
        
        return jsonify({"ok": True, "reports": reports})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/profile")
def profile():
    """
    Graph-backed profile view.
    Accepts optional ?email=<email|upn|id>. If missing, uses session user email.
    Data fetched from Microsoft Graph (presence/directReports/user fields).
    """
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))

    token = session.get("access_token")
    if not token:
        flash("No Graph access token in session.", "error")
        return redirect(url_for("dashboard"))

    email = (request.args.get("email") or session["user"].get("email") or "").strip()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        select = [
            "id", "displayName", "jobTitle", "aboutMe", "mail", "officeLocation",
            "mobilePhone", "companyName", "city", "employeeId", "createdDateTime",
            "userPrincipalName", "businessPhones", "department"
        ]
        user = _fetch_user(email, headers, select)
        if not user:
            flash("User not found in Graph.", "error")
            return redirect(url_for("dashboard"))

        uid = user.get("id") or user.get("userPrincipalName") or user.get("mail")

        # presence (best-effort)
        presence = None
        try:
            pres_url = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(uid)}/presence"
            pr = _graph_get(pres_url, headers)
            if pr.status_code == 200:
                presence = pr.json()
        except Exception:
            presence = None

        # reports to (manager)
        reports_to = None
        try:
            mgr = _fetch_manager(uid, headers)
            if mgr:
                mgr_id = mgr.get("id") or mgr.get("userPrincipalName") or mgr.get("mail")
                if mgr_id:
                    mgr_full = _fetch_user(
                        mgr_id, headers,
                        ["id", "displayName", "mail", "userPrincipalName", "jobTitle", "department"]
                    ) or mgr
                    reports_to = _slim_person(mgr_full)
        except Exception:
            reports_to = None

        # direct reports (full list for the section)
        direct_reports: List[Dict[str, Any]] = []
        try:
            dr = _fetch_direct_reports(
                uid, headers,
                ["id", "displayName", "mail", "userPrincipalName", "jobTitle", "department"]
            )
            direct_reports = [_slim_person(x) for x in (dr or [])]
            # stable ordering
            direct_reports.sort(key=lambda x: (x.get("displayName") or x.get("mail") or "").lower())
        except Exception:
            direct_reports = []

        # pinned people = top direct reports (existing behavior)
        pinned = direct_reports[:6] if direct_reports else []

        text_src = " ".join([
            str(user.get("aboutMe") or ""),
            str(user.get("jobTitle") or ""),
            str(user.get("companyName") or ""),
            str(user.get("department") or ""),
        ])
        skills = _heuristic_extract_skills(text_src, limit=8)

        # active_days from createdDateTime
        active_days = None
        created = user.get("createdDateTime")
        if created:
            try:
                dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                active_days = (datetime.datetime.utcnow() - dt).days
            except Exception:
                active_days = None

        # --- "Works With" (Colleagues) and "Teams Groups" ---
        works_with = []
        try:
            # person/people endpoint provides high-relevance coworkers
            purl = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(uid)}/people?$top=6"
            presp = requests.get(purl, headers=headers, verify=VERIFY_SSL)
            if presp.status_code == 200:
                pdata = presp.json().get("value", [])
                for p in pdata:
                    works_with.append({
                        "id": p.get("id"),
                        "displayName": p.get("displayName"),
                        "jobTitle": p.get("jobTitle"),
                        "mail": p.get("userPrincipalName") or p.get("id"),
                        "department": p.get("department")
                    })
        except Exception:
            works_with = []

        member_of = []
        try:
            # transitive memberOf includes Groups and Teams. 
            # Filter for unified groups (Office 365/Teams) and capture visibility.
            murl = f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(uid)}/transitiveMemberOf/microsoft.graph.group?$top=20&$select=id,displayName,description,visibility,groupTypes"
            mresp = requests.get(murl, headers=headers, verify=VERIFY_SSL)
            if mresp.status_code == 200:
                mdata = mresp.json().get("value", [])
                for m in mdata:
                    # 'Unified' groupType usually indicates a Team or O365 group
                    is_private = (m.get("visibility") or "").lower() == "private"
                    member_of.append({
                        "id": m.get("id"),
                        "displayName": m.get("displayName"),
                        "description": m.get("description"),
                        "visibility": m.get("visibility"),
                        "is_private": is_private
                    })
        except Exception:
            member_of = []

        # status: presence if available, otherwise heuristic
        status = "inactive"
        presence_status = "Offline"
        presence_color = "gray"
        
        if presence and presence.get("availability"):
            avail = presence.get("availability")
            presence_map = {
                'Available': ('Available', 'green'),
                'Busy': ('Busy', 'red'),
                'DoNotDisturb': ('Do Not Disturb', 'red'),
                'Away': ('Away', 'yellow'),
                'BeRightBack': ('Be Right Back', 'yellow'),
                'Offline': ('Offline', 'gray'),
                'PresenceUnknown': ('Offline', 'gray')
            }
            presence_status, presence_color = presence_map.get(avail, ('Offline', 'gray'))
            
            avail_lower = avail.lower()
            if avail_lower and all(x not in avail_lower for x in ("away", "dnd", "offline")):
                status = "active"
        else:
            if active_days is not None and active_days < 3650 and (user.get("jobTitle") or user.get("companyName")):
                status = "active"

        # simple activity timeline (years since created)
        activity_timeline = []
        try:
            if created:
                dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                start_year = dt.year
                now_year = datetime.datetime.utcnow().year
                for y in range(start_year, now_year + 1):
                    activity_timeline.append(1 + (y - start_year))
            else:
                activity_timeline = [1, 2, 3, 4, 5]
        except Exception:
            activity_timeline = [1, 2, 3]

        profile_obj = {
            "id": user.get("id"),
            "presence_status": presence_status,
            "presence_color": presence_color,
            "displayName": user.get("displayName"),
            "jobTitle": user.get("jobTitle"),
            "aboutMe": user.get("aboutMe") or "",
            "mail": user.get("mail") or user.get("userPrincipalName"),
            "officeLocation": user.get("officeLocation"),
            "mobilePhone": user.get("mobilePhone"),
            "companyName": user.get("companyName"),
            "city": user.get("city"),
            "businessPhones": user.get("businessPhones") or [],
            "createdDateTime": user.get("createdDateTime"),
            "presence": presence,

            "reports_to": reports_to,
            "direct_reports": direct_reports,

            "pinned": pinned,
            "skills": [s["name"] for s in skills],
            "skills_weights": skills,
            "active_days": active_days,
            "status": status,
            "activity_timeline": activity_timeline,
            "skills_summary": _summarize_skills_fallback(skills, user.get("jobTitle") or "", user.get("aboutMe") or ""),

            "works_with": works_with,
            "member_of_groups": member_of,
        }

        return render_template("profile.html", profile=profile_obj)

    except Exception as e:
        app.logger.exception("Failed to build profile from Graph")
        flash(f"Failed to fetch profile: {e}", "error")
        return redirect(url_for("dashboard"))


def _jwt_payload_noverify(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        return json.loads(payload_json)
    except Exception:
        return {}


def _token_expiry_epoch(token: str) -> Optional[int]:
    payload = _jwt_payload_noverify(token)
    exp = payload.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


@app.route("/token/refresh", methods=["POST"])
def token_refresh():
    """
    Accept a new access token, validate it against /me, and update session.
    Returns JSON so UI can show a popup and refresh without full logout.
    """
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401

    data = request.get_json(silent=True) or {}
    new_token = (data.get("token") or "").strip()
    if not new_token:
        return {"ok": False, "error": "Token required"}, 400

    try:
        resp = graph_get_me(new_token)
    except Exception as e:
        return {"ok": False, "error": f"Graph call failed: {e}"}, 400

    if resp.status_code != 200:
        return {"ok": False, "error": "Invalid token"}, 400

    me = resp.json() or {}
    graph_mail = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    expected = (session["user"].get("email") or "").lower()
    if not expected or graph_mail != expected:
        return {"ok": False, "error": "Token does not belong to current user"}, 400

    session["access_token"] = new_token
    exp = _token_expiry_epoch(new_token)
    if exp:
        session["token_exp"] = exp
        session["token_expires_in_sec"] = max(0, exp - int(time.time()))
    else:
        session.pop("token_exp", None)
        session.pop("token_expires_in_sec", None)

    return {"ok": True, "token_exp": session.get("token_exp")}, 200


@app.before_request
def _block_if_token_expired():
    """
    If token is expired, block Graph pages but allow:
    - login/logout
    - token refresh endpoint
    - static files
    - dashboard (so modal can open + refresh can be submitted)
    - profile_photo_user (so images don't break while refreshing)
    """
    if not session.get("user"):
        return

    endpoint = (request.endpoint or "")
    if endpoint in {"login", "logout", "token_refresh", "static", "root", "dashboard", "profile_photo_user"}:
        return

    token_exp = session.get("token_exp")
    if token_exp is None:
        return  # unknown expiry -> don't block

    try:
        if int(time.time()) >= int(token_exp):
            # For API refresh call return JSON, otherwise redirect to dashboard (popup will handle)
            if request.path.startswith("/token/"):
                return {"ok": False, "error": "Token expired"}, 401
            flash("Access token expired. Please enter a new token to continue.", "error")
            return redirect(url_for("dashboard"))
    except Exception:
        return


# ------------------------------
# Insights page (Company overview)
# ------------------------------


def _normalize_city_name(value: Any) -> str:
    """Normalize city values so the same city isn't double-counted.

    Handles common data quality issues:
    - Leading/trailing whitespace
    - Mixed case ("bengaluru" vs "Bengaluru")
    - Multiple spaces / tabs
    - Common punctuation noise

    Notes:
    - We *don't* try to geocode or merge truly different names (e.g. "NYC" vs "New York").
    - Empty/None returns "" and callers can map it to (Unassigned).
    """

    if value is None:
        return ""

    s = str(value).strip()
    if not s:
        return ""

    # Collapse repeated whitespace to a single space
    s = " ".join(s.split())

    # Remove a few common trailing punctuation chars that create duplicate buckets
    s = s.rstrip(",.;")

    # --- Known alias merges (org-specific data hygiene) ---
    # Apply on a lower-cased, punctuation-trimmed key so "Bangalore", "bangalore",
    # "Bangalore," all land in the same bucket.
    alias_key = s.casefold()
    CITY_ALIASES = {
        # Bangalore / Bengaluru
        "bangalore": "Bengaluru",
        "bengaluru": "Bengaluru",
    }
    if alias_key in CITY_ALIASES:
        return CITY_ALIASES[alias_key]

    # Title-case for display (keeps words readable), but preserve all-caps acronyms.
    # If there are 2+ letters and they're all uppercase, keep as-is.
    letters = [ch for ch in s if ch.isalpha()]
    if len(letters) >= 2 and all(ch.isupper() for ch in letters):
        return s
    return s.title()


def _insights_fetch_dept_location_city_country_counts(
    headers: Dict[str, str], max_items: int = 0
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Return ({department: count}, {officeLocation: count}, {city: count}, {country: count}) for the whole tenant.

    Uses server-side paging over /users selecting only department + officeLocation + city + country.
    max_items=0 means fetch all (up to a safety ceiling).
    """
    select = "department,officeLocation,city,country"
    url = f"https://graph.microsoft.com/v1.0/users?$select={urllib.parse.quote(select)}&$top=999"
    h = dict(headers)
    h.setdefault("Prefer", "odata.maxpagesize=999")

    ceiling = 50000 if not max_items else max_items
    users = _graph_get_paged_raise(url, h, max_items=ceiling)

    dept_counts: Dict[str, int] = defaultdict(int)
    loc_counts: Dict[str, int] = defaultdict(int)
    city_counts: Dict[str, int] = defaultdict(int)
    country_counts: Dict[str, int] = defaultdict(int)
    for u in users:
        dept = (u.get("department") or "").strip() or "(Unassigned)"
        loc = (u.get("officeLocation") or "").strip() or "(Unassigned)"
        city = _normalize_city_name(u.get("city")) or "(Unassigned)"
        country = _normalize_country_name(u.get("country")) or "(Unassigned)"
        dept_counts[dept] += 1
        loc_counts[loc] += 1
        city_counts[city] += 1
        country_counts[country] += 1
    return dict(dept_counts), dict(loc_counts), dict(city_counts), dict(country_counts)


def _insights_fetch_users_min_fields(headers: Dict[str, str], max_items: int = 0) -> List[Dict[str, str]]:
    """Return list of users with only the fields needed for client-side country summary.

    We deliberately keep this small to avoid shipping PII. The UI needs only department/location/city/country.
    """
    select = "department,officeLocation,city,country"
    url = f"https://graph.microsoft.com/v1.0/users?$select={urllib.parse.quote(select)}&$top=999"
    h = dict(headers)
    h.setdefault("Prefer", "odata.maxpagesize=999")

    ceiling = 50000 if not max_items else max_items
    users = _graph_get_paged_raise(url, h, max_items=ceiling)

    out: List[Dict[str, str]] = []
    for u in users:
        out.append(
            {
                "department": (u.get("department") or "").strip(),
                "officeLocation": (u.get("officeLocation") or "").strip(),
                "city": _normalize_city_name(u.get("city")),
                "country": _normalize_country_name(u.get("country")) or "",
            }
        )
    return out


def _normalize_country_name(value: Any) -> str:
    """Normalize country display values.

    Goals:
    - Don't show country *codes* like "IN"/"US" in the UI when a name mapping is known.
    - Merge duplicates like "IN" and "India" into "India".
    - Keep it conservative: if we can't confidently map a value, return the cleaned original.
    """
    raw = "" if value is None else str(value)
    s = raw.strip()
    if not s:
        return ""

    # If the value already looks like a name (not a short alpha code), keep it.
    if len(s) > 3 and not re.fullmatch(r"[A-Za-z]{2,3}", s):
        return s

    code = s.upper()

    # Fast path for common non-ISO aliases.
    alias = {
        "UK": "GB",
    }
    code = alias.get(code, code)

    if pycountry is None:
        return s

    @lru_cache(maxsize=512)
    def _lookup(c: str) -> Optional[str]:
        try:
            if len(c) == 2:
                obj = pycountry.countries.get(alpha_2=c)
            elif len(c) == 3:
                obj = pycountry.countries.get(alpha_3=c)
            else:
                obj = None
            if not obj:
                return None
            return getattr(obj, "name", None) or getattr(obj, "official_name", None)
        except Exception:
            return None

    name = _lookup(code)
    return name or s


@app.route("/insights", methods=["GET"])
def insights():
    if not session.get("user"):
        return redirect(url_for("root", _anchor="login"))
    # Keep initial render fast; the page JS will also fetch the name from /insights/data.
    return render_template("insights.html", user=session.get("user"), company_name=ORG_NAME)


@app.route("/insights/data", methods=["GET"])
def insights_data():
    if not session.get("user"):
        return {"ok": False, "error": "Not logged in"}, 401
    token = session.get("access_token")
    if not token:
        return {"ok": False, "error": "No token"}, 400

    headers = _graph_headers_from_token(token)

    # Cache results briefly to avoid paging the entire tenant on every refresh.
    # Note: This is per-process in-memory cache.
    bust = (request.args.get("bust") or "").strip() == "1"
    cache = getattr(app, "_insights_cache", None)
    if cache is None:
        cache = {"ts": 0, "rows": None, "company": None, "company_ts": 0}
        setattr(app, "_insights_cache", cache)

    now = time.time()
    ttl_sec = 300  # 5 minutes
    if (not bust) and cache.get("rows") and (now - float(cache.get("ts") or 0) < ttl_sec):
        rows = cache["rows"]
        loc_rows = cache.get("loc_rows") or []
        city_rows = cache.get("city_rows") or []
        country_rows = cache.get("country_rows") or []
        users_min = cache.get("users_min") or []
    else:
        # Fetch independent datasets in parallel so the client doesn't wait on sequential Graph paging.
        # This is safe because each task is read-only and uses its own requests calls.
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                f_counts = ex.submit(_insights_fetch_dept_location_city_country_counts, headers, 0)
                f_users = ex.submit(_insights_fetch_users_min_fields, headers, 0)
                f_company = ex.submit(_insights_fetch_company_name, headers)

                dept_counts, loc_counts, city_counts, country_counts = f_counts.result()
                users_min = f_users.result()
                company_name = f_company.result() or ORG_NAME
        except Exception as e:
            app.logger.exception("Insights fetch failed")
            return {"ok": False, "error": str(e)}, 500

        rows = [
            {"department": k, "count": int(v)}
            for k, v in sorted(dept_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            if k != "(Unassigned)"
        ]
        loc_rows = [
            {"location": k, "count": int(v)}
            for k, v in sorted(loc_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            if k != "(Unassigned)"
        ]
        city_rows = [
            {"city": k, "count": int(v)}
            for k, v in sorted(city_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            if k != "(Unassigned)"
        ]
        country_rows = [
            {"country": k, "count": int(v)}
            for k, v in sorted(country_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
            if k != "(Unassigned)"
        ]
        cache["rows"] = rows
        cache["loc_rows"] = loc_rows
        cache["city_rows"] = city_rows
        cache["country_rows"] = country_rows
        cache["users_min"] = users_min
        cache["ts"] = now

        # If we fetched company_name in parallel above, persist it to cache too.
        if company_name:
            cache["company"] = company_name
            cache["company_ts"] = now

    # Company name: cache separately (cheap call but still avoid repeating).
    company_name: Optional[str] = None
    try:
        # If we already cached company_name above (parallel fetch), use it.
        if cache.get("company") and (now - float(cache.get("company_ts") or 0) < ttl_sec):
            company_name = cache.get("company")
        else:
            company_name = _insights_fetch_company_name(headers) or ORG_NAME
            cache["company"] = company_name
            cache["company_ts"] = now
    except Exception:
        company_name = ORG_NAME

    # Total employees should reflect the true total (including "(Unassigned)").
    # We hide "(Unassigned)" rows in the breakdown lists, but the KPI should not undercount.
    # Note: `dept_counts` is only available in the cache-miss branch, so we recompute totals here.
    def _sum_counts(list_rows):
        try:
            return int(sum(int(r.get("count") or 0) for r in (list_rows or [])))
        except Exception:
            return 0

    # Best: compute from the raw user rows (includes unassigned/blank values).
    # This is robust because `users_min` is always present (from cache or fresh fetch).
    if users_min:
        total = int(len(users_min))
    else:
        # Fallback: best-effort, based on any of the aggregate lists.
        total = max(
            _sum_counts(rows),
            _sum_counts(loc_rows),
            _sum_counts(city_rows),
            _sum_counts(country_rows),
        )
    return {
        "ok": True,
        "company_name": company_name or ORG_NAME,
        "total_employees": total,
        "department_count": len(rows),
        "departments": rows,
        "locations": loc_rows,
        "cities": city_rows,
        "countries": country_rows,
        "users": users_min,
    }, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
