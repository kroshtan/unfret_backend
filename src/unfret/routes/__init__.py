from fastapi import APIRouter

from unfret.routes.admin import router as admin_router
from unfret.routes.analyze import router as analyze_router
from unfret.routes.check import router as check_router
from unfret.routes.credits import router as credits_router
from unfret.routes.extension import router as extension_router
from unfret.routes.images import router as images_router
from unfret.routes.keys import router as keys_router
from unfret.routes.pages import router as pages_router
from unfret.routes.probes import router as probes_router
from unfret.routes.reports import router as reports_router
from unfret.routes.session import router as session_router
from unfret.routes.showcase import router as showcase_router

router = APIRouter()
router.include_router(session_router)
router.include_router(keys_router)
router.include_router(credits_router)
router.include_router(analyze_router)
router.include_router(reports_router)
router.include_router(check_router)
router.include_router(probes_router)
router.include_router(pages_router)
router.include_router(showcase_router)
router.include_router(extension_router)
router.include_router(images_router)
router.include_router(admin_router)

__all__ = ["router"]
