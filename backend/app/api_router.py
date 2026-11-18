"""Register the client, back-office and supplier HTTP routes."""

from fastapi import APIRouter

from app.modules.account.admin_router import router as account_admin
from app.modules.account.client_router import router as account_client
from app.modules.analytics.admin_router import router as analytics_admin
from app.modules.billing.admin_router import router as billing_admin
from app.modules.billing.client_router import router as billing_client
from app.modules.billing.webhook_router import router as billing_webhooks
from app.modules.content.admin_router import router as content_admin
from app.modules.content.catalog_admin_router import router as catalog_admin
from app.modules.content.client_router import router as content_client
from app.modules.content.media_admin_router import router as media_admin
from app.modules.content.partner_router import router as content_partner
from app.modules.content.review_router import router as reviews
from app.modules.content.webhook_router import router as content_webhooks
from app.modules.ops.api_key_router import router as admin_api_keys
from app.modules.ops.config_admin_router import router as config_admin
from app.modules.ops.config_client_router import router as config_client
from app.modules.ops.partner_router import router as partner
from app.modules.ops.settlement_router import router as settlements
from app.modules.ops.staff_router import router as staff
from app.modules.ops.supplier_admin_router import router as suppliers
from app.modules.playback.client_router import router as playback_client
from app.modules.recommend.client_router import router as feed
from app.modules.social.admin_router import router as social_admin
from app.modules.social.client_router import router as social_client
from app.modules.wallet.admin_router import router as wallet_admin
from app.modules.wallet.client_router import router as wallet_client

api_router = APIRouter()

admin_api = APIRouter(prefix="/admin/v1")
admin_api.include_router(staff)
admin_api.include_router(suppliers)
admin_api.include_router(catalog_admin)
admin_api.include_router(admin_api_keys)
admin_api.include_router(content_admin)
admin_api.include_router(media_admin)
admin_api.include_router(config_admin)
admin_api.include_router(account_admin)
admin_api.include_router(billing_admin)
admin_api.include_router(social_admin)
admin_api.include_router(wallet_admin)
admin_api.include_router(analytics_admin)

partner_api = APIRouter(prefix="/partner/v1")
partner_api.include_router(partner)
partner_api.include_router(content_partner)

client_api = APIRouter(prefix="/v1")
client_api.include_router(content_client)
client_api.include_router(playback_client)
client_api.include_router(wallet_client)
client_api.include_router(account_client)
client_api.include_router(billing_client)
client_api.include_router(billing_webhooks)
client_api.include_router(config_client)
client_api.include_router(social_client)
client_api.include_router(content_webhooks)
client_api.include_router(feed)

api_router.include_router(admin_api)
api_router.include_router(partner_api)
api_router.include_router(reviews, prefix=admin_api.prefix)
api_router.include_router(settlements, prefix=admin_api.prefix)
api_router.include_router(client_api)
