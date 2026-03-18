import requests
import time

from odoo import _
from odoo.exceptions import UserError


class ShopifyAPI:
    def __init__(self, shop_url, access_token, api_key=None, api_secret=None):
        self.shop_url = (shop_url or "").rstrip("/")
        self.access_token = access_token
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "%s/admin/api/2024-01" % self.shop_url
        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }

    def _request(self, method, path, params=None, data=None, max_retries=5):
        """Low-level JSON request helper with robust rate limit handling.

        When Shopify responds with HTTP 429, we respect the Retry-After header
        when present and otherwise fall back to an exponential backoff using
        2 ** retry_count, up to a maximum of 5 retries.
        """
        url = "%s%s" % (self.base_url, path)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    params=params,
                    json=data,
                    timeout=30,
                )
            except requests.RequestException as e:
                raise UserError(_("Shopify request error: %s") % e)

            # Handle rate limiting (HTTP 429) with exponential backoff
            if response.status_code == 429 and attempt <= max_retries:
                retry_after = response.headers.get("Retry-After")
                # retry_count is the number of retries already performed
                retry_count = attempt - 1
                try:
                    if retry_after:
                        sleep_seconds = int(retry_after)
                    else:
                        sleep_seconds = 2 ** max(retry_count, 1)
                except Exception:
                    sleep_seconds = 2 ** max(retry_count, 1)
                time.sleep(sleep_seconds)
                continue

            if not response.ok:
                err_msg = response.text
                try:
                    body = response.json()
                    if isinstance(body, dict):
                        errors = body.get("errors") or body.get("error")
                        if errors:
                            err_msg = (
                                errors
                                if isinstance(errors, str)
                                else ", ".join(
                                    str(e)
                                    for e in (
                                        errors
                                        if isinstance(errors, list)
                                        else [errors]
                                    )
                                )
                            )
                except Exception:
                    pass
                raise UserError(
                    _("Shopify API error %s: %s")
                    % (response.status_code, err_msg)
                )

            if response.text:
                return response.json()
            return {}

    def _paginate(self, path, root_key, params=None, max_retries=5):
        """Cursor-based pagination using Shopify Link headers with rate limit handling."""
        all_items = []
        url = "%s%s" % (self.base_url, path)
        query_params = params or {}

        while url:
            attempt = 0
            while True:
                attempt += 1
                try:
                    response = requests.get(
                        url,
                        headers=self.headers,
                        params=query_params,
                        timeout=30,
                    )
                except requests.RequestException as e:
                    raise UserError(_("Shopify request error: %s") % e)

                # Rate limit handling for paginated calls
                if response.status_code == 429 and attempt <= max_retries:
                    retry_after = response.headers.get("Retry-After")
                    retry_count = attempt - 1
                    try:
                        if retry_after:
                            sleep_seconds = int(retry_after)
                        else:
                            sleep_seconds = 2 ** max(retry_count, 1)
                    except Exception:
                        sleep_seconds = 2 ** max(retry_count, 1)
                    time.sleep(sleep_seconds)
                    continue

                if not response.ok:
                    raise UserError(
                        _("Shopify API error %s: %s")
                        % (response.status_code, response.text)
                    )

                data = response.json() if response.text else {}
                items = data.get(root_key, [])
                all_items.extend(items)

                # After first page, subsequent requests should rely on the next link only
                query_params = {}
                url = self._extract_next_link(response.headers)
                break

        return all_items

    @staticmethod
    def _extract_next_link(headers):
        """Extract next page URL from Shopify Link header."""
        link_header = headers.get("Link") or headers.get("link")
        if not link_header:
            return None

        # Example: <https://shop.myshopify.com/admin/api/2024-01/products.json?page_info=xxx&limit=250>; rel="next"
        parts = [p.strip() for p in link_header.split(",")]
        for part in parts:
            if 'rel="next"' in part:
                url_part = part.split(";")[0].strip()
                if url_part.startswith("<") and url_part.endswith(">"):
                    return url_part[1:-1]
        return None

    def ping(self):
        self.get_products(limit=1)
        return True

    def get_products(self, **params):
        params.setdefault("limit", 250)
        return self._paginate("/products.json", "products", params=params)

    def get_orders(self, **params):
        params.setdefault("limit", 250)
        return self._paginate("/orders.json", "orders", params=params)

    def get_customers(self, **params):
        params.setdefault("limit", 250)
        return self._paginate("/customers.json", "customers", params=params)

    def get_inventory(self, **params):
        res = self._request("GET", "/inventory_levels.json", params=params)
        return res.get("inventory_levels", [])

    def update_inventory_level(self, inventory_item_id, available, location_id):
        data = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available": int(available),
        }
        return self._request("POST", "/inventory_levels/set.json", data=data)

    def get_locations(self, **params):
        """Fetch all locations from Shopify."""
        params.setdefault("limit", 250)
        return self._paginate("/locations.json", "locations", params=params)

    # Product import helpers used by product import workflow
    def get_products_by_date(self, **params):
        """Fetch products filtered by date fields (created_at_min / updated_at_min).

        Pagination is handled transparently via _paginate.
        """
        params.setdefault("limit", 250)
        return self._paginate("/products.json", "products", params=params)

    def get_product_by_id(self, product_id):
        """Fetch a single product by Shopify ID."""
        path = "/products/%s.json" % product_id
        return self._request("GET", path)

    def get_customer_by_id(self, customer_id):
        """Fetch a single customer by Shopify ID."""
        path = "/customers/%s.json" % customer_id
        return self._request("GET", path)

    def create_product(self, product_payload):
        """Create a product in Shopify. product_payload follows REST Product resource.
        Returns API response with created product (id, variants, etc.).
        """
        return self._request("POST", "/products.json", data={"product": product_payload})

    def update_product(self, shopify_product_id, product_payload):
        """Update an existing product in Shopify."""
        path = "/products/%s.json" % shopify_product_id
        return self._request("PUT", path, data={"product": product_payload})

    def update_variant(self, variant_id, variant_payload):
        """Update an existing product variant in Shopify.

        Expected payload format (REST):
            {"variant": {"id": VARIANT_ID, "price": "123.45", ...}}
        """
        path = "/variants/%s.json" % variant_id
        return self._request("PUT", path, data=variant_payload)

    # Product image helpers
    def get_product_images(self, product_id, **params):
        """Fetch all images for a given Shopify product."""
        path = "/products/%s/images.json" % product_id
        res = self._request("GET", path, params=params)
        return res.get("images", [])

    def delete_product_image(self, product_id, image_id):
        """Delete a specific image from a Shopify product."""
        path = "/products/%s/images/%s.json" % (product_id, image_id)
        # Shopify returns 200 with empty body on successful delete
        return self._request("DELETE", path)

    def create_product_image(self, product_id, image_payload):
        """Create a new image on a Shopify product using base64 attachment.

        Expected payload format:
            {
                "image": {
                    "attachment": "<BASE64_IMAGE_DATA>"
                }
            }
        """
        path = "/products/%s/images.json" % product_id
        return self._request("POST", path, data=image_payload)

