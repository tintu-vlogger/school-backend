from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # "development" enables dev conveniences (seeded test logins, reset codes in the
    # API response when no SMTP is configured). Anything else is treated as production.
    APP_ENV: str = "development"

    POSTGRES_USER: str = "hrms"
    POSTGRES_PASSWORD: str  # no default: must come from the environment
    POSTGRES_DB: str = "hrms"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str  # no default
    # Database index, so several apps can share one Redis server without key clashes.
    REDIS_DB: int = 0

    SECRET_KEY: str  # no default; 32+ random characters

    # Outgoing email (password resets). Leave SMTP_HOST empty in development.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TLS: bool = True
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    DATABASE_URL: str = ""

    EVOLUTION_API_KEY: str = ""
    EVOLUTION_API_PORT: int = 8080
    EVOLUTION_API_URL: str = "http://evolution-api:8080"

    BACKEND_URL: str = "http://backend:8000"

    # Base URL Evolution API uses to call our webhook. This is a container-to-container
    # call, so it must stay on the Docker network name -- BACKEND_URL may be overridden
    # to a browser-reachable host (e.g. http://localhost:8001) for OAuth callbacks and
    # short links, which is not resolvable from inside the evolution-api container.
    WEBHOOK_BASE_URL: str = "http://backend:8000"
    # Official WhatsApp Cloud API (Meta). The app secret lets us verify webhook signatures.
    META_APP_SECRET: str = ""
    # Embedded Signup ("Continue with Facebook"): the platform's Meta app id and the
    # Embedded Signup configuration id from Facebook Login for Business.
    META_APP_ID: str = ""
    META_CONFIG_ID: str = ""

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Extra browser origins allowed by CORS, comma separated (e.g. a Railway frontend domain).
    CORS_ORIGINS: str = ""

    # Public web address of the frontend, used in links sent to people.
    FRONTEND_URL: str = "http://localhost:3001"
    # First platform admin, created at startup when the platform_users table is empty.
    # Required in production; development falls back to admin@campusly.dev / Platform9#admin.
    PLATFORM_ADMIN_EMAIL: str = ""
    PLATFORM_ADMIN_PASSWORD: str = ""
    # Unprivileged database role the API switches to when it connects as a superuser,
    # so row-level security applies (superusers skip it). Empty turns this off.
    DB_APP_ROLE: str = "school_app"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() not in ("development", "dev", "local")

    def check(self) -> None:
        """Refuse to start production with placeholder secrets."""
        if not self.is_production:
            return
        problems = []
        if len(self.SECRET_KEY) < 32 or "change" in self.SECRET_KEY.lower():
            problems.append("SECRET_KEY must be at least 32 random characters")
        for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
            if "secret_2024" in getattr(self, name) or len(getattr(self, name)) < 12:
                problems.append(f"{name} is a placeholder or too short")
        if problems:
            raise RuntimeError("Unsafe configuration for production: " + "; ".join(problems))

    @property
    def database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://default:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
