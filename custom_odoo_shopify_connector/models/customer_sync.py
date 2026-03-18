from odoo import api, models, _
from odoo.exceptions import UserError
from ..services.shopify_api import ShopifyAPI


class ShopifyCustomerSync(models.AbstractModel):
    _name = "shopify.customer.sync"
    _description = "Shopify Customer Synchronization"

    @api.model
    def sync_customers(self, store):
        api_client = ShopifyAPI(
            shop_url=store.shop_url,
            access_token=store.access_token,
            api_key=store.api_key,
            api_secret=store.api_secret,
        )

        try:
            customers = api_client.get_customers()
        except Exception as e:
            self.env["shopify.sync.log.mixin"].create_log(
                store=store,
                log_type="customer",
                message=str(e),
                payload=False,
                status="failed",
            )
            return

        Partner = self.env["res.partner"]

        for c in customers:
            self._import_customer_payload(store, c)

        self.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="customer",
            message=_("Customers synchronized from Shopify."),
            payload=False,
            status="success",
        )

    def _get_country_id(self, country_code):
        if not country_code:
            return False
        country = self.env["res.country"].search(
            [("code", "=", country_code)], limit=1
        )
        return country.id or False

    def _get_state_id(self, state_code, country_code):
        if not state_code or not country_code:
            return False
        country = self.env["res.country"].search(
            [("code", "=", country_code)], limit=1
        )
        if not country:
            return False
        state = self.env["res.country.state"].search(
            [("code", "=", state_code), ("country_id", "=", country.id)],
            limit=1,
        )
        return state.id or False

    @api.model
    def import_customer_by_id(self, store, customer_id):
        """Fetch a single customer from Shopify by ID and create/update in Odoo."""
        api_client = ShopifyAPI(
            shop_url=store.shop_url,
            access_token=store.access_token,
            api_key=store.api_key,
            api_secret=store.api_secret,
        )
        try:
            response = api_client.get_customer_by_id(customer_id)
        except Exception as e:
            self.env["shopify.sync.log.mixin"].create_log(
                store=store,
                log_type="customer",
                message=str(e),
                payload=False,
                status="failed",
            )
            raise
        customer = response.get("customer") or response
        if not customer or not customer.get("id"):
            raise UserError(_("Customer not found in Shopify."))
        self._import_customer_payload(store, customer)
        self.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="customer",
            message=_("Customer %s imported.") % customer_id,
            payload=False,
            status="success",
        )

    def _import_customer_payload(self, store, c):
        """Create or update a single partner from Shopify customer payload."""
        Partner = self.env["res.partner"]
        email = c.get("email")
        first_name = c.get("first_name") or ""
        last_name = c.get("last_name") or ""
        phone = c.get("phone") or False
        name = (first_name + " " + last_name).strip() or email or _("Shopify Customer")
        partner = False
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner and phone:
            partner = Partner.search([("phone", "=", phone)], limit=1)
        address = False
        addresses = c.get("addresses") or []
        if addresses:
            address = addresses[0]
        vals = {"name": name, "email": email, "phone": phone}
        if address:
            vals.update({
                "street": address.get("address1"),
                "street2": address.get("address2"),
                "city": address.get("city"),
                "zip": address.get("zip"),
                "country_id": self._get_country_id(address.get("country_code")),
                "state_id": self._get_state_id(
                    address.get("province_code"), address.get("country_code")
                ),
            })
        if partner:
            partner.write(vals)
        else:
            Partner.create(vals)

    @api.model
    def cron_sync_customers(self):
        stores = self.env["shopify.store"].search([("active", "=", True)])
        for store in stores:
            self.sync_customers(store)

