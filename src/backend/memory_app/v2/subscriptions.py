"""Subscription HTTP delivery; OAuth and model execution remain domain services."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from core.storage_provider import SQLiteUnitOfWorkConflict
from ..chatgpt_subscription import SubscriptionError
from ..model_config import ModelConfigurationError


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt


class Selection(Revision):
    model: StrictStr | None
    allow_remote: StrictBool | None = None
    expected_generation_revision: StrictInt | None = None


def install_subscription_routes(application, *, models):
    subscriptions = getattr(models, "subscriptions", None)
    if subscriptions is None:
        return
    router = APIRouter(prefix="/api/v2/settings/subscriptions")

    def call(fn, **kwargs):
        try:
            return fn(**kwargs)
        except SubscriptionError as error:
            raise HTTPException(error.status, error.code) from None
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, "subscription_revision_conflict") from None
        except ModelConfigurationError as error:
            raise HTTPException(409 if "conflict" in str(error) or "changed" in str(error) else 400, str(error)) from None

    @router.get("")
    def status():
        generation = models.public()["generation"]
        return {**subscriptions.status(), "selection": models.subscription_selection(),
                "allow_remote": generation["allow_remote"], "generation_revision": generation["revision"],
                "selection_ready": generation["configured"] if generation.get("subscription_binding") else False}

    @router.post("/login")
    def login(body: Revision):
        return call(subscriptions.login, expected_revision=body.expected_revision)

    @router.get("/login/{attempt_id}")
    def progress(attempt_id: str):
        return call(subscriptions.attempt, attempt_id=attempt_id)

    @router.delete("/login/{attempt_id}")
    def cancel(attempt_id: str):
        return call(subscriptions.cancel, attempt_id=attempt_id)

    @router.post("/logout")
    def logout(body: Revision):
        return call(subscriptions.logout, expected_revision=body.expected_revision)

    @router.get("/models")
    def catalog():
        return {"items": call(subscriptions.models)}

    @router.patch("/selection")
    def select(body: Selection):
        return call(models.select_subscription, **body.model_dump())

    application.include_router(router)
    application.router.on_shutdown.append(models.close)
