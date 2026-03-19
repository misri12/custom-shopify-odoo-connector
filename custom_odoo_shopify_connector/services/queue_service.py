from odoo import fields

from .product_service import ProductService


def _shopify_datetime(dt):
    """Format Odoo Datetime for Shopify API (ISO 8601)."""
    if not dt:
        return None
    return fields.Datetime.to_string(dt).replace(" ", "T") + "Z"


class ProductQueueService:
    """Service layer for Shopify -> Odoo product queue management (batch queue + lines)."""

    def __init__(self, env):
        self.env = env

    def _build_product_params(self, import_based_on, from_date, to_date):
        """Build API params for date filtering; empty means fetch all products."""
        params = {}
        if import_based_on == "create_date":
            min_key, max_key = "created_at_min", "created_at_max"
        else:
            min_key, max_key = "updated_at_min", "updated_at_max"
        if from_date:
            params[min_key] = _shopify_datetime(from_date)
        if to_date:
            params[max_key] = _shopify_datetime(to_date)
        return params

    def enqueue_products_by_date(self, store, import_based_on, from_date, to_date):
        """Fetch products from Shopify; create ONE queue and multiple lines."""
        api_client = store._get_api_client()
        params = self._build_product_params(import_based_on, from_date, to_date)
        products = api_client.get_products(**params)

        Queue = self.env["shopify.product.queue"]
        Line = self.env["shopify.product.queue.line"]

        queue = Queue.create({"store_id": store.id})

        for product in products:
            self._create_queue_line(Line, queue.id, product)

        return queue

    def fetch_products_into_queue(self, queue, from_date=None, to_date=None):
        """Fetch products from Shopify and add them as lines to an existing queue.
        If from_date/to_date are not set, fetches all products.
        """
        queue.ensure_one()
        store = queue.store_id
        api_client = store._get_api_client()
        import_based_on = "create_date"
        params = self._build_product_params(import_based_on, from_date, to_date)
        products = api_client.get_products(**params)

        Line = self.env["shopify.product.queue.line"]
        existing_ids = set(queue.line_ids.mapped("shopify_product_id"))
        added = 0
        for product in products:
            pid = str(product.get("id") or "")
            if pid and pid not in existing_ids:
                self._create_queue_line(Line, queue.id, product)
                existing_ids.add(pid)
                added += 1

        return added

    def _create_queue_line(self, Line, queue_id, product):
        """Create one queue line from a Shopify product payload."""
        variants = product.get("variants") or []
        variant = variants[0] if variants else {}
        sku = variant.get("sku") or ""
        price = float(variant.get("price") or 0.0)
        Line.create(
            {
                "queue_id": queue_id,
                "shopify_product_id": str(product.get("id") or ""),
                "shopify_sku": sku,
                "title": product.get("title") or "",
                "price": price,
                "state": "draft",
            }
        )

    def process_queue(self, queue):
        """Process all draft lines of the given queue."""
        Line = queue.env["shopify.product.queue.line"]
        draft_lines = queue.line_ids.filtered(lambda l: l.state == "draft")
        for line in draft_lines:
            self._process_line(line)

    def _process_line(self, line):
        """Process a single queue line: fetch product from Shopify and import into Odoo."""
        queue = line.queue_id
        store = queue.store_id
        api_client = store._get_api_client()

        try:
            product_response = api_client.get_product_by_id(line.shopify_product_id)
            product_payload = product_response.get("product") or product_response

            product_service = ProductService(self.env)
            product_service.import_shopify_product(product_payload, store)

            line.state = "done"
            line.message = "Processed successfully."
        except Exception as e:
            line.state = "failed"
            line.message = str(e)

    def process_draft_queues(self):
        """Process all queues that have draft lines (used by cron)."""
        Queue = self.env["shopify.product.queue"]
        queues_with_draft = Queue.search([
            ("line_ids.state", "=", "draft"),
        ], order="create_date asc")
        for queue in queues_with_draft:
            self.process_queue(queue)
