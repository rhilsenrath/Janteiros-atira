"""Parse Atira runtime access once; resolve roles without website IDs or URL defaults."""
import json
import os
from urllib.parse import urlsplit
from pydantic import Field, SecretStr
from core.contracts import Record
from tools.logging import redact

ROLES = ("tender", "supplier", "erp", "mail")

class SiteAccess(Record):
    site_id: str
    url: str
    username: SecretStr = Field(repr=False, exclude=True)
    password: SecretStr = Field(repr=False, exclude=True)
    description: str = ""

    def public(self):
        return {"site_id": self.site_id, "url": self.url, "description": self.description}

class RoleAssignments(Record):
    tender: str
    supplier: str
    erp: str
    mail: str

class AccessManager:
    def __init__(self, runtime, env=None):
        env = os.environ if env is None else env
        raw = runtime.get("access")
        if raw is None:
            try:
                raw = json.loads(env.get("CHALLENGE_SITE_ACCESS", "[]"))
            except (TypeError, ValueError):
                raise ValueError("CHALLENGE_SITE_ACCESS must be a JSON array") from None
        if not isinstance(raw, list) or len(raw) != 4:
            raise ValueError("Four runtime website access entries required")
        self.sites = {}
        for item in raw:
            try:
                site_id, url = item["siteId"], item["url"]
                credentials = item["credentials"]
                username, password = credentials["username"], credentials["password"]
                parsed = urlsplit(url)
                if not all(isinstance(v, str) and v for v in (site_id, url, username, password)):
                    raise ValueError()
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError()
                if site_id in self.sites:
                    raise ValueError()
                self.sites[site_id] = SiteAccess(site_id=site_id, url=url, username=username,
                    password=password, description=str(item.get("description", item.get("name", ""))))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Invalid runtime website access entry (values redacted)") from None
        self.assignments = None

    async def resolve(self, llm, brief):
        response = await llm.ask(
            "Assign the four website entries to business roles tender, supplier, erp and mail. "
            "Use the supplied runtime brief, descriptions, URLs and IDs; each ID exactly once. "
            "Do not invent a website. IDs and layouts can differ between scenarios.",
            redact(
                json.dumps({"brief": brief, "websites": [s.public() for s in self.sites.values()]}), self.secrets()),
            output_type=RoleAssignments)
        assignments = response.structured
        if not isinstance(assignments, RoleAssignments) or set(assignments.model_dump().values()) != set(self.sites):
            raise ValueError("Model could not uniquely assign runtime websites")
        self.assignments = assignments

    def for_role(self, role):
        if role not in ROLES or self.assignments is None:
            raise ValueError("Runtime website roles have not been resolved")
        return self.sites[getattr(self.assignments, role)]

    def secrets(self):
        return [value.get_secret_value() for site in self.sites.values()
                for value in (site.username, site.password)]
