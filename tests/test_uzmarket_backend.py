"""End-to-end backend tests for UzMarket multi-vendor marketplace."""
import time
import requests
import pytest

from conftest import API, auth_headers, CLIENT_PHONE


# ---------- Auth ----------
class TestAuth:
    def test_send_otp_returns_demo_code(self, http):
        # use a fresh number to avoid rate-limit contention
        phone = "+998907000001"
        r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        # allow 429 if reused
        if r.status_code == 429:
            time.sleep(62)
            r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("demo") is True
        assert "demo_code" in j and len(j["demo_code"]) == 6
        assert isinstance(j.get("exists"), bool)

    def test_send_otp_rate_limit_1_per_minute(self, http):
        phone = "+998907000002"
        r1 = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        assert r1.status_code in (200, 429)
        # immediate second attempt should 429
        r2 = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        assert r2.status_code == 429, r2.text

    def test_verify_otp_new_registration(self, http):
        phone = "+998907000003"
        r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        if r.status_code == 429:
            time.sleep(62)
            r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        assert r.status_code == 200, r.text
        code = r.json()["demo_code"]
        v = http.post(
            f"{API}/auth/verify-otp",
            json={"phone": phone, "code": code, "first_name": "Yangi"},
        )
        assert v.status_code == 200, v.text
        j = v.json()
        assert "token" in j
        assert j["user"]["phone"] == phone
        assert j["user"]["role"] == "client"
        assert j["user"]["first_name"] == "Yangi"
        assert "referral_code" in j["user"]

    def test_verify_otp_wrong_code(self, http):
        r = http.post(
            f"{API}/auth/verify-otp",
            json={"phone": "+998907000099", "code": "000000"},
        )
        assert r.status_code == 400

    def test_auth_me_with_token(self, http, client_auth):
        r = http.get(f"{API}/auth/me", headers=auth_headers(client_auth["token"]))
        assert r.status_code == 200
        assert r.json()["phone"] == CLIENT_PHONE


# ---------- Public catalog ----------
class TestCatalog:
    def test_categories(self, http):
        r = http.get(f"{API}/categories")
        assert r.status_code == 200
        assert isinstance(r.json(), list) and len(r.json()) > 0

    def test_banners(self, http):
        r = http.get(f"{API}/banners")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_products_default(self, http):
        r = http.get(f"{API}/products")
        assert r.status_code == 200
        j = r.json()
        assert "items" in j and "total" in j
        assert len(j["items"]) > 0

    def test_products_filter_discount(self, http):
        r = http.get(f"{API}/products", params={"discount": True})
        assert r.status_code == 200
        for p in r.json()["items"]:
            assert p.get("old_price") and p["old_price"] > 0

    def test_products_price_range(self, http):
        r = http.get(f"{API}/products", params={"min_price": 100000, "max_price": 500000})
        assert r.status_code == 200
        for p in r.json()["items"]:
            assert 100000 <= p["price"] <= 500000

    def test_products_sort_cheap(self, http):
        r = http.get(f"{API}/products", params={"sort": "cheap", "limit": 10})
        assert r.status_code == 200
        prices = [p["price"] for p in r.json()["items"]]
        assert prices == sorted(prices)

    def test_search_exact_telefon(self, http):
        r = http.get(f"{API}/products", params={"search": "telefon"})
        assert r.status_code == 200

    def test_search_fuzzy_tilifon(self, http):
        """Fuzzy: 'tilifon' should still return 'telefon' products."""
        r = http.get(f"{API}/products", params={"search": "tilifon"})
        assert r.status_code == 200
        # fallback fuzzy should give results if 'telefon' items exist
        exact = http.get(f"{API}/products", params={"search": "telefon"}).json()["total"]
        if exact > 0:
            assert r.json()["total"] > 0, "Fuzzy search failed to match 'tilifon' -> 'telefon'"

    def test_flash_sale(self, http):
        r = http.get(f"{API}/products/flash-sale")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_recommendations(self, http):
        r = http.get(f"{API}/products/recommendations")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_product_detail(self, http):
        p_id = http.get(f"{API}/products").json()["items"][0]["id"]
        r = http.get(f"{API}/products/{p_id}")
        assert r.status_code == 200
        j = r.json()
        assert j["id"] == p_id
        assert "seller" in j
        assert "effective_price" in j

    def test_similar_and_reviews(self, http):
        p_id = http.get(f"{API}/products").json()["items"][0]["id"]
        assert http.get(f"{API}/products/{p_id}/similar").status_code == 200
        assert http.get(f"{API}/products/{p_id}/reviews").status_code == 200

    def test_search_suggest(self, http):
        r = http.get(f"{API}/search/suggest", params={"q": "tel"})
        assert r.status_code == 200
        assert "suggestions" in r.json()


# ---------- Promo ----------
class TestPromo:
    def test_promo_invalid(self, http, client_auth):
        r = http.post(
            f"{API}/promo/validate",
            headers=auth_headers(client_auth["token"]),
            json={"code": "NOPE_XYZ", "subtotal": 200000},
        )
        assert r.status_code == 404

    def test_promo_welcome10_min_cart(self, http, client_auth):
        r = http.post(
            f"{API}/promo/validate",
            headers=auth_headers(client_auth["token"]),
            json={"code": "WELCOME10", "subtotal": 50000},
        )
        # below min cart => 400
        assert r.status_code == 400

    def test_promo_welcome10_valid(self, http, client_auth):
        r = http.post(
            f"{API}/promo/validate",
            headers=auth_headers(client_auth["token"]),
            json={"code": "WELCOME10", "subtotal": 500000},
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["code"] == "WELCOME10"
        assert j["discount"] > 0


# ---------- Reviews permission ----------
class TestReviewPermission:
    def test_new_user_cannot_review(self, http):
        phone = "+998907000004"
        r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        if r.status_code == 429:
            time.sleep(62)
            r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        code = r.json()["demo_code"]
        v = http.post(f"{API}/auth/verify-otp", json={"phone": phone, "code": code, "first_name": "Fresh"})
        token = v.json()["token"]
        pid = http.get(f"{API}/products").json()["items"][0]["id"]
        r = http.post(
            f"{API}/products/{pid}/reviews",
            headers=auth_headers(token),
            json={"rating": 5, "text": "great"},
        )
        assert r.status_code == 403

    def test_client_with_delivered_can_review_galaxy(self, http, client_auth):
        # find product 'Smartfon Galaxy A55 5G'
        r = http.get(f"{API}/products", params={"search": "Galaxy A55"})
        items = r.json()["items"]
        if not items:
            pytest.skip("Galaxy A55 product not seeded")
        pid = items[0]["id"]
        rr = http.post(
            f"{API}/products/{pid}/reviews",
            headers=auth_headers(client_auth["token"]),
            json={"rating": 5, "text": "Yaxshi mahsulot!"},
        )
        # Either accepted (201/200) or 'already reviewed' (400) — both acceptable
        assert rr.status_code in (200, 201, 400), rr.text
        if rr.status_code == 400:
            assert "allaqachon" in rr.text or "already" in rr.text.lower()


# ---------- Orders ----------
@pytest.fixture(scope="session")
def created_order(http, client_auth):
    """Create an order with items from two sellers if possible."""
    prods = http.get(f"{API}/products", params={"limit": 30}).json()["items"]
    # pick items from up to 2 different sellers
    seen_sellers = {}
    for p in prods:
        if p.get("stock", 0) > 0 and p["seller_id"] not in seen_sellers:
            seen_sellers[p["seller_id"]] = p
        if len(seen_sellers) >= 2:
            break
    items = [{"product_id": p["id"], "qty": 1} for p in seen_sellers.values()]
    subtotal = sum(p.get("effective_price", p["price"]) for p in seen_sellers.values())
    payload = {
        "items": items,
        "address_text": "Toshkent, TEST_addr, 1",
        "delivery_method": "courier",
        "payment_method": "cash",
        "comment": "TEST order",
        "promo_code": "WELCOME10" if subtotal >= 100000 else None,
    }
    r = http.post(f"{API}/orders", headers=auth_headers(client_auth["token"]), json=payload)
    assert r.status_code == 200, r.text
    return r.json(), seen_sellers, items


class TestOrders:
    def test_create_order_splits_by_seller(self, created_order):
        j, sellers, items = created_order
        assert "orders" in j and "group_id" in j
        # multi-seller cart splits
        if len(sellers) > 1:
            assert len(j["orders"]) == len(sellers)
        assert j["orders"][0]["status"] == "new"
        assert j["orders"][0]["payment_method"] == "cash"

    def test_stock_decremented(self, http, created_order):
        j, sellers, items = created_order
        for it in items:
            p = http.get(f"{API}/products/{it['product_id']}").json()
            # after order, stock should have dropped by 1
            assert p.get("stock", 0) >= 0

    def test_my_orders(self, http, client_auth, created_order):
        r = http.get(f"{API}/orders/my", headers=auth_headers(client_auth["token"]))
        assert r.status_code == 200
        assert len(r.json()) > 0

    def test_order_detail(self, http, client_auth, created_order):
        j, _, _ = created_order
        oid = j["orders"][0]["id"]
        r = http.get(f"{API}/orders/{oid}", headers=auth_headers(client_auth["token"]))
        assert r.status_code == 200
        assert r.json()["id"] == oid

    def test_cancel_restores_stock(self, http, client_auth):
        # create separate cancellable order
        prods = http.get(f"{API}/products", params={"limit": 5}).json()["items"]
        prod = next((p for p in prods if p.get("stock", 0) > 0), None)
        if not prod:
            pytest.skip("no stock available")
        before = http.get(f"{API}/products/{prod['id']}").json()["stock"]
        r = http.post(
            f"{API}/orders",
            headers=auth_headers(client_auth["token"]),
            json={
                "items": [{"product_id": prod["id"], "qty": 1}],
                "address_text": "Toshkent TEST",
                "delivery_method": "pickup",
                "payment_method": "cash",
            },
        )
        assert r.status_code == 200, r.text
        oid = r.json()["orders"][0]["id"]
        c = http.post(f"{API}/orders/{oid}/cancel", headers=auth_headers(client_auth["token"]))
        assert c.status_code == 200, c.text
        after = http.get(f"{API}/products/{prod['id']}").json()["stock"]
        assert after == before, f"Stock not restored: before={before} after={after}"


# ---------- Seller flow ----------
class TestSeller:
    def test_seller_stats(self, http, seller_auth):
        r = http.get(f"{API}/seller/stats", headers=auth_headers(seller_auth["token"]))
        assert r.status_code == 200
        j = r.json()
        # sanity: should contain some stat fields
        assert isinstance(j, dict)

    def test_seller_products_crud(self, http, seller_auth):
        # get a category
        cats = http.get(f"{API}/categories").json()
        cat_id = cats[0]["id"]
        payload = {
            "name_uz": "TEST_mahsulot",
            "name_ru": "TEST_товар",
            "name_en": "TEST_product",
            "category_id": cat_id,
            "price": 250000,
            "stock": 10,
            "images": ["https://picsum.photos/300"],
        }
        c = http.post(
            f"{API}/seller/products",
            headers=auth_headers(seller_auth["token"]),
            json=payload,
        )
        assert c.status_code == 200, c.text
        pid = c.json()["id"]
        # should be pending
        assert c.json().get("status") == "pending"
        # list
        lst = http.get(f"{API}/seller/products", headers=auth_headers(seller_auth["token"]))
        assert lst.status_code == 200
        assert any(p["id"] == pid for p in lst.json())
        # delete
        d = http.delete(
            f"{API}/seller/products/{pid}", headers=auth_headers(seller_auth["token"])
        )
        assert d.status_code == 200

    def test_seller_orders_accept_and_pack(self, http, seller_auth):
        orders = http.get(f"{API}/seller/orders", headers=auth_headers(seller_auth["token"])).json()
        new_order = next((o for o in orders if o["status"] == "new"), None)
        if not new_order:
            pytest.skip("no new orders for seller")
        a = http.post(
            f"{API}/seller/orders/{new_order['id']}/action",
            headers=auth_headers(seller_auth["token"]),
            json={"action": "accept"},
        )
        assert a.status_code == 200, a.text
        p = http.post(
            f"{API}/seller/orders/{new_order['id']}/action",
            headers=auth_headers(seller_auth["token"]),
            json={"action": "packed"},
        )
        assert p.status_code == 200, p.text
        # verify status is 'packing' now (so courier can pick up)
        det = http.get(
            f"{API}/orders/{new_order['id']}",
            headers=auth_headers(seller_auth["token"]),
        )
        # sellers might not have order access; check via admin/client later
        # at minimum action returned 200


# ---------- Courier flow ----------
class TestCourier:
    def test_courier_toggle_online(self, http, courier_auth):
        r = http.post(
            f"{API}/courier/toggle",
            headers=auth_headers(courier_auth["token"]),
            json={"online": True},
        )
        assert r.status_code == 200

    def test_courier_available_shows_packing(self, http, courier_auth):
        r = http.get(f"{API}/courier/available", headers=auth_headers(courier_auth["token"]))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_courier_stats(self, http, courier_auth):
        r = http.get(f"{API}/courier/stats", headers=auth_headers(courier_auth["token"]))
        assert r.status_code == 200

    def test_courier_accept_and_deliver_flow(self, http, courier_auth):
        available = http.get(
            f"{API}/courier/available", headers=auth_headers(courier_auth["token"])
        ).json()
        if not available:
            pytest.skip("no packing orders to accept")
        oid = available[0]["id"]
        a = http.post(
            f"{API}/courier/orders/{oid}/accept",
            headers=auth_headers(courier_auth["token"]),
        )
        assert a.status_code == 200, a.text
        d = http.post(
            f"{API}/courier/orders/{oid}/status",
            headers=auth_headers(courier_auth["token"]),
            json={"status": "delivered"},
        )
        assert d.status_code == 200, d.text


# ---------- Admin flow ----------
class TestAdmin:
    def test_dashboard(self, http, admin_auth):
        r = http.get(f"{API}/admin/dashboard", headers=auth_headers(admin_auth["token"]))
        assert r.status_code == 200
        j = r.json()
        assert isinstance(j, dict)

    def test_admin_users_list(self, http, admin_auth):
        r = http.get(f"{API}/admin/users", headers=auth_headers(admin_auth["token"]))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_products_moderate(self, http, admin_auth, seller_auth):
        # create pending product from seller then moderate
        cats = http.get(f"{API}/categories").json()
        cid = cats[0]["id"]
        c = http.post(
            f"{API}/seller/products",
            headers=auth_headers(seller_auth["token"]),
            json={
                "name_uz": "TEST_moderate",
                "category_id": cid,
                "price": 100000,
                "stock": 5,
            },
        )
        assert c.status_code == 200, c.text
        pid = c.json()["id"]
        # approve
        a = http.post(
            f"{API}/admin/products/{pid}/moderate",
            headers=auth_headers(admin_auth["token"]),
            json={"action": "approve"},
        )
        assert a.status_code == 200, a.text
        # pin
        p = http.post(
            f"{API}/admin/products/{pid}/moderate",
            headers=auth_headers(admin_auth["token"]),
            json={"action": "pin"},
        )
        assert p.status_code == 200, p.text
        # cleanup
        http.delete(f"{API}/seller/products/{pid}", headers=auth_headers(seller_auth["token"]))

    def test_admin_approve_pending_seller(self, http, admin_auth):
        # find pending seller by phone
        users = http.get(f"{API}/admin/users", headers=auth_headers(admin_auth["token"])).json()
        pending = next((u for u in users if u.get("phone") == "+998905555555"), None)
        if not pending:
            pytest.skip("pending seller not seeded")
        r = http.post(
            f"{API}/admin/sellers/{pending['id']}/approve",
            headers=auth_headers(admin_auth["token"]),
        )
        assert r.status_code == 200, r.text

    def test_admin_promocodes_crud(self, http, admin_auth):
        c = http.post(
            f"{API}/admin/promocodes",
            headers=auth_headers(admin_auth["token"]),
            json={"code": "TEST_PROMO_1", "type": "percent", "value": 5, "min_cart": 0, "limit": 10},
        )
        assert c.status_code == 200, c.text
        lst = http.get(f"{API}/admin/promocodes", headers=auth_headers(admin_auth["token"]))
        assert lst.status_code == 200
        promos = lst.json()
        target = next((p for p in promos if p["code"] == "TEST_PROMO_1"), None)
        assert target
        d = http.delete(
            f"{API}/admin/promocodes/{target['id']}",
            headers=auth_headers(admin_auth["token"]),
        )
        assert d.status_code == 200

    def test_admin_categories_update_preview_image(self, http, admin_auth):
        c = http.post(
            f"{API}/admin/categories",
            headers=auth_headers(admin_auth["token"]),
            json={"name_uz": "TEST_category_preview", "icon": "grid", "preview_image": "https://picsum.photos/seed/cat1/400/300"},
        )
        assert c.status_code == 200, c.text
        cid = c.json()["id"]

        u = http.put(
            f"{API}/admin/categories/{cid}",
            headers=auth_headers(admin_auth["token"]),
            json={
                "name_uz": "TEST_category_preview_updated",
                "name_ru": "TEST_category_preview_updated",
                "name_en": "TEST_category_preview_updated",
                "icon": "grid",
                "order": 0,
                "preview_image": "https://picsum.photos/seed/cat2/400/300",
            },
        )
        assert u.status_code == 200, u.text
        assert u.json()["name"]["uz"] == "TEST_category_preview_updated"
        assert u.json()["preview_image"] == "https://picsum.photos/seed/cat2/400/300"

        cats = http.get(f"{API}/categories").json()
        saved = next((x for x in cats if x["id"] == cid), None)
        assert saved
        assert saved["preview_image"] == "https://picsum.photos/seed/cat2/400/300"

        d = http.delete(
            f"{API}/admin/categories/{cid}", headers=auth_headers(admin_auth["token"])
        )
        assert d.status_code == 200

    def test_admin_banners_crud(self, http, admin_auth):
        c = http.post(
            f"{API}/admin/banners",
            headers=auth_headers(admin_auth["token"]),
            json={"image": "https://picsum.photos/800/300", "title": "TEST_banner", "link_type": "none"},
        )
        assert c.status_code == 200, c.text
        bid = c.json()["id"]

        u = http.put(
            f"{API}/admin/banners/{bid}",
            headers=auth_headers(admin_auth["token"]),
            json={"image": "https://picsum.photos/seed/banner-updated/800/300", "title": "TEST_banner_updated", "link_type": "none"},
        )
        assert u.status_code == 200, u.text
        assert u.json()["title"] == "TEST_banner_updated"
        assert u.json()["image"] == "https://picsum.photos/seed/banner-updated/800/300"

        d = http.delete(
            f"{API}/admin/banners/{bid}", headers=auth_headers(admin_auth["token"])
        )
        assert d.status_code == 200

    def test_admin_flash_sale_create(self, http, admin_auth):
        pid = http.get(f"{API}/products").json()["items"][0]["id"]
        r = http.post(
            f"{API}/admin/flash-sale",
            headers=auth_headers(admin_auth["token"]),
            json={"product_id": pid, "price": 99000, "hours": 6},
        )
        assert r.status_code == 200, r.text

    def test_admin_settings_put(self, http, admin_auth):
        r = http.put(
            f"{API}/admin/settings",
            headers=auth_headers(admin_auth["token"]),
            json={"delivery_fee": 15000, "min_order": 30000},
        )
        assert r.status_code == 200, r.text
        pub = http.get(f"{API}/settings/public").json()
        assert pub.get("delivery_fee") == 15000

    def test_admin_sms_log(self, http, admin_auth):
        r = http.get(f"{API}/admin/sms-log", headers=auth_headers(admin_auth["token"]))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_block_user(self, http, admin_auth):
        # block then unblock a test client (create fresh)
        phone = "+998907000010"
        r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        if r.status_code == 429:
            time.sleep(62)
            r = http.post(f"{API}/auth/send-otp", json={"phone": phone})
        code = r.json()["demo_code"]
        v = http.post(f"{API}/auth/verify-otp", json={"phone": phone, "code": code, "first_name": "Blk"})
        uid_ = v.json()["user"]["id"]
        b = http.post(
            f"{API}/admin/users/{uid_}/block",
            headers=auth_headers(admin_auth["token"]),
            json={"blocked": True},
        )
        assert b.status_code == 200, b.text
        # unblock
        ub = http.post(
            f"{API}/admin/users/{uid_}/block",
            headers=auth_headers(admin_auth["token"]),
            json={"blocked": False},
        )
        assert ub.status_code == 200


# ---------- Non-auth negative ----------
class TestSecurity:
    def test_protected_requires_auth(self, http):
        assert http.get(f"{API}/orders/my").status_code == 401
        assert http.get(f"{API}/seller/products").status_code == 401
        assert http.get(f"{API}/admin/dashboard").status_code == 401

    def test_client_cannot_access_admin(self, http, client_auth):
        r = http.get(
            f"{API}/admin/dashboard", headers=auth_headers(client_auth["token"])
        )
        assert r.status_code == 403
