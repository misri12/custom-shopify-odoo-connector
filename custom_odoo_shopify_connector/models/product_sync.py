from odoo import api, models, _

from ..services.shopify_api import ShopifyAPI
from ..services.product_service import ProductService
from .sync_log import ShopifySyncLogMixin


class ShopifyProductSync(ShopifySyncLogMixin):
    _name = "shopify.product.sync"
    _description = "Shopify Product Synchronization"

    @api.model
    def sync_products(self, store):
        api_client = ShopifyAPI(
            shop_url=store.shop_url,
            access_token=store.access_token,
            api_key=store.api_key,
            api_secret=store.api_secret,
        )

        try:
            products = api_client.get_products()
        except Exception as e:
            self.env["shopify.sync.log.mixin"].create_log(
                store=store,
                log_type="product",
                message=str(e),
                payload=False,
                status="failed",
            )
            return

        product_service = ProductService(self.env)

        for product_payload in products:
            try:
                product_service.import_shopify_product(product_payload, store)
            except Exception as e:
                self.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="product",
                    message=str(e),
                    payload=product_payload,
                    status="failed",
                )

        self.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="product",
            message=_("Products synchronized from Shopify."),
            payload=False,
            status="success",
        )

    @api.model
    def cron_sync_products(self):
        stores = self.env["shopify.store"].search([("active", "=", True)])
        for store in stores:
            self.sync_products(store)

