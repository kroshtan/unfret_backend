from fastapi import APIRouter

from veridex.routes.analyze import router as analyze_router
from veridex.routes.check import router as check_router
from veridex.routes.credits import router as credits_router
from veridex.routes.extension import router as extension_router
from veridex.routes.pages import router as pages_router
from veridex.routes.probes import router as probes_router
from veridex.routes.session import router as session_router

router = APIRouter()
router.include_router(session_router)
router.include_router(credits_router)
router.include_router(analyze_router)
router.include_router(check_router)
router.include_router(probes_router)
router.include_router(pages_router)
router.include_router(extension_router)

__all__ = ["router"]
