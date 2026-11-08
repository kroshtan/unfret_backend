from fastapi import APIRouter

from veridex.routes.accounts import router as accounts_router
from veridex.routes.admin import router as admin_router
from veridex.routes.analyze import router as analyze_router
from veridex.routes.check import router as check_router
from veridex.routes.paddle import router as paddle_router
from veridex.routes.probes import router as probes_router

router = APIRouter()
router.include_router(accounts_router)
router.include_router(admin_router)
router.include_router(analyze_router)
router.include_router(check_router)
router.include_router(probes_router)
router.include_router(paddle_router)

__all__ = ["router"]
