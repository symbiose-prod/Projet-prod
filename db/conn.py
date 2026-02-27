# db/conn.py
import os
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, quote_plus
from typing import Any, Mapping, Optional, Tuple

from sqlalchemy import create_engine, text as _text
from sqlalchemy.engine import Engine, Result


# ------------------------
# Helpers URL
# ------------------------
def _is_internal(host: str | None) -> bool:
    # Host interne Kubernetes chez Kinsta
    return bool(host) and host.endswith(".svc.cluster.local")


def _normalize_scheme(db_url: str) -> str:
    """
    - Remplace postgres:// par postgresql:// (SQLAlchemy 2.x)
    - Ajoute le driver psycopg2 si absent.
    """
    u = urlparse(db_url)
    scheme = u.scheme

    # 1) Remap 'postgres' -> 'postgresql'
    if scheme == "postgres":
        scheme = "postgresql"

    # 2) Ajoute '+psycopg2' s'il n'est pas déjà là
    if scheme == "postgresql":
        scheme = "postgresql+psycopg2"

    return urlunparse((scheme, u.netloc, u.path, u.params, u.query, u.fragment))


def _with_param(url: str, key: str, value: str) -> str:
    u = urlparse(url)
    qs = dict(parse_qsl(u.query, keep_blank_values=True))
    qs[key] = value
    new_query = urlencode(qs)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_query, u.fragment))


def _get_db_parts() -> tuple:
    """Lit les variables d'env et retourne (host, port, name, user, pwd). Valide les requises."""
    host = os.getenv("DB_HOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("DB_PORT") or os.getenv("POSTGRES_PORT") or "5432"
    name = os.getenv("DB_DATABASE") or os.getenv("DB_NAME") or os.getenv("POSTGRES_DB")
    user = os.getenv("DB_USERNAME") or os.getenv("DB_USER") or os.getenv("POSTGRES_USER")
    pwd  = os.getenv("DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD") or ""

    missing = []
    if not host: missing.append("DB_HOST")
    if not name: missing.append("DB_DATABASE")
    if not user: missing.append("DB_USERNAME")
    if missing:
        raise RuntimeError(f"Variables DB manquantes : {', '.join(missing)}")

    return host, port, name, user, pwd


def _make_url(host: str, port: str, name: str, user: str, pwd: str, sslmode: str) -> str:
    """Construit une URL PostgreSQL avec le mot de passe URL-encodé."""
    pwd_enc = quote_plus(pwd) if pwd else ""
    user_enc = quote_plus(user) if user else ""
    return f"postgresql+psycopg2://{user_enc}:{pwd_enc}@{host}:{port}/{name}?sslmode={sslmode}"


def _build_url() -> str:
    # 0) Si l'admin force un sslmode via l'env, on reconstruit l'URL
    forced_ssl = os.getenv("DB_SSLMODE")
    if forced_ssl:
        host, port, name, user, pwd = _get_db_parts()
        return _make_url(host, port, name, user, pwd, forced_ssl)

    # 1) Si une URL complète est fournie
    raw = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
    if raw:
        url = _normalize_scheme(raw)  # postgres:// -> postgresql+psycopg2://
        host = urlparse(url).hostname
        if _is_internal(host):
            url = _with_param(url, "sslmode", "disable")
        else:
            if "sslmode=" not in url:
                url = _with_param(url, "sslmode", "require")
        return url

    # 2) Fallback : reconstruire à partir des morceaux
    host, port, name, user, pwd = _get_db_parts()
    sslmode = "disable" if _is_internal(host) else "require"
    return _make_url(host, port, name, user, pwd, sslmode)


# ------------------------
# Engine SQLAlchemy
# ------------------------
_ENGINE: Engine | None = None

def get_engine() -> Engine:
    """Renvoie un Engine SQLAlchemy (singleton)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(_build_url(), pool_pre_ping=True, future=True)
    return _ENGINE

# Alias backward-compat si ailleurs tu fais `from db import engine`
def engine() -> Engine:  # noqa: N802 - garder le nom historique
    return get_engine()


# ------------------------
# Exécution SQL
# ------------------------
# db/conn.py — patch run_sql

from typing import Any, Mapping, Optional, List, Dict, Union
from sqlalchemy import text as _text

def run_sql(sql: Any, params: Optional[Mapping[str, Any]] = None) -> Union[int, list[dict]]:
    """
    Exécute une requête SQL (str ou sqlalchemy TextClause).
    - Si la requête retourne des lignes (SELECT...), renvoie une liste de dicts.
    - Sinon (INSERT/UPDATE/DELETE...), renvoie le rowcount (int).
    """
    if isinstance(sql, str):
        sql = _text(sql)

    with get_engine().begin() as conn:
        result = conn.execute(sql, params or {})
        if result.returns_rows:
            # Convertit chaque Row en dict via le mapping colonne->valeur
            return [dict(row._mapping) for row in result.fetchall()]
        else:
            return result.rowcount


def ping() -> Tuple[bool, str]:
    """Test de santé : SELECT 1."""
    try:
        _ = run_sql("SELECT 1;")
        return True, "✅ DB OK (SELECT 1)"
    except Exception as e:
        return False, f"❌ Erreur de connexion : {e}"


# ------------------------
# Debug helpers (sans secrets)
# ------------------------
def _current_dsn() -> str:
    """DSN complet effectivement utilisé (avec mot de passe masqué)."""
    url = _build_url()
    u = urlparse(url)
    # masque le mot de passe
    netloc = u.netloc
    if "@" in netloc and ":" in netloc.split("@")[0]:
        user_pw, hostpart = netloc.split("@", 1)
        user = user_pw.split(":", 1)[0]
        netloc = f"{user}:***@{hostpart}"
    return urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment))


def debug_dsn() -> str:
    """Petit résumé sans secret: host + sslmode."""
    u = urlparse(_build_url())
    qs = dict(parse_qsl(u.query))
    return f"host={u.hostname} | sslmode={qs.get('sslmode', '<none>')}"


def whoami() -> str:
    """Retourne l'utilisateur utilisé par le DSN."""
    u = urlparse(_build_url())
    user = (u.username or "<none>")
    return f"user={user}"
