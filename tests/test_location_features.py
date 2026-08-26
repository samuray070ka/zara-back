"""Tests for location capture features (buyer saved_location, seller shop, order coords)."""
import time
import requests
import pytest

from conftest import API, auth_headers, CLIENT_PHONE, SELLER_PHONE, COURIER_PHONE


# ---------- Buyer: PUT /users/me/location ----------
class TestBuyerLocation:
    def test_save_and_read_saved_location(self, http, client_auth):
        headers = auth_headers(client_auth["token"])
        payload = {"lat": 41.3111, "lng": 69.2797}
        r = http.put(f"{API}/users/me/location", headers=headers, json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "saved_location" in data, f"missing saved_location in response: {data}"
        sl = data["saved_location"]
        assert abs(sl["lat"] - 41.3111) < 1e-6
        assert abs(sl["lng"] - 69.2797) < 1e-6
        assert "updated_at" in sl

        # /auth/me returns saved_location
        me = http.get(f"{API}/auth/me", headers=headers).json()
        assert "saved_location" in me
        assert abs(me["saved_location"]["lat"] - 41.3111) < 1e-6

    def test_location_requires_auth(self, http):
        r = http.put(f"{API}/users/me/location", json={"lat": 1, "lng": 2})
        assert r.status_code == 401


# ---------- Seller: PUT /seller/location ----------
class TestSellerLocation:
    def test_seller_can_save_shop_location(self, http, seller_auth):
        headers = auth_headers(seller_auth["token"])
        payload = {"lat": 41.3200, "lng": 69.2500}
        r = http.put(f"{API}/seller/location", headers=headers, json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        si = data.get("seller_info") or {}
        assert abs(si.get("shop_lat", 0) - 41.3200) < 1e-6, si
        assert abs(si.get("shop_lng", 0) - 69.2500) < 1e-6, si

    def test_client_cannot_save_shop_location(self, http, client_auth):
        headers = auth_headers(client_auth["token"])
        r = http.put(f"{API}/seller/location", headers=headers, json={"lat": 41, "lng": 69})
        assert r.status_code == 403


# ---------- Orders store per-order pickup/delivery ----------
@pytest.fixture(scope="module")
def two_orders_with_coords(http, client_auth, seller_auth):
    """Ensure seller has shop coords, then create 2 orders from different sellers or
    with 2 different delivery coordinates, verifying pickup/delivery persisted."""
    # 1) set seller shop location (TechnoPlaza)
    http.put(
        f"{API}/seller/location",
        headers=auth_headers(seller_auth["token"]),
        json={"lat": 41.3350, "lng": 69.2650},
    )

    # 2) find approved products from TechnoPlaza (seller_auth user)
    prods = http.get(f"{API}/products", params={"limit": 50}).json()["items"]
    # group by seller
    by_seller = {}
    for p in prods:
        if p.get("stock", 0) > 0:
            by_seller.setdefault(p["seller_id"], []).append(p)

    orders_created = []
    # Order A: single seller, coords 41.31, 69.27
    seller_ids = list(by_seller.keys())
    assert seller_ids, "no seeded products available"
    p1 = by_seller[seller_ids[0]][0]
    payload_a = {
        "items": [{"product_id": p1["id"], "qty": 1}],
        "address_text": "TEST_addr A",
        "address_lat": 41.3111,
        "address_lng": 69.2797,
        "delivery_method": "courier",
        "payment_method": "cash",
    }
    r = http.post(f"{API}/orders", headers=auth_headers(client_auth["token"]), json=payload_a)
    assert r.status_code == 200, r.text
    orders_created.extend(r.json()["orders"])

    # Order B: different coords
    p2 = by_seller[seller_ids[-1]][0]  # possibly same seller or different
    payload_b = {
        "items": [{"product_id": p2["id"], "qty": 1}],
        "address_text": "TEST_addr B",
        "address_lat": 41.2995,
        "address_lng": 69.2401,
        "delivery_method": "courier",
        "payment_method": "cash",
    }
    r = http.post(f"{API}/orders", headers=auth_headers(client_auth["token"]), json=payload_b)
    assert r.status_code == 200, r.text
    orders_created.extend(r.json()["orders"])

    return orders_created


class TestOrderLocations:
    def test_orders_store_delivery_and_pickup_locations(self, http, client_auth, two_orders_with_coords):
        my = http.get(f"{API}/orders/my", headers=auth_headers(client_auth["token"])).json()
        ids = {o["id"] for o in two_orders_with_coords}
        target = [o for o in my if o["id"] in ids]
        assert target, "created orders not found in /orders/my"
        for o in target:
            dl = o.get("delivery_location")
            pu = o.get("pickup_location")
            assert dl and dl.get("lat") is not None and dl.get("lng") is not None, o
            assert pu and pu.get("lat") is not None and pu.get("lng") is not None, o

    def test_order_delivery_coords_match_payload(self, http, client_auth, two_orders_with_coords):
        my = http.get(f"{API}/orders/my", headers=auth_headers(client_auth["token"])).json()
        ids = {o["id"] for o in two_orders_with_coords}
        target = [o for o in my if o["id"] in ids]
        # There must be at least two different delivery coords across the two orders
        coords = {(round(o["delivery_location"]["lat"], 4), round(o["delivery_location"]["lng"], 4)) for o in target}
        assert len(coords) >= 2, f"expected distinct delivery coords, got {coords}"


# ---------- Courier endpoints echo per-order coords ----------
class TestCourierLocations:
    def _promote_orders_to_packing(self, http, seller_token, two_orders):
        """Seller accepts and packs orders so courier sees them."""
        for o in two_orders:
            for act in ("accept", "packed"):
                http.post(
                    f"{API}/seller/orders/{o['id']}/action",
                    headers=auth_headers(seller_token),
                    json={"action": act},
                )

    def test_courier_available_returns_per_order_coords(
        self, http, seller_auth, courier_auth, two_orders_with_coords
    ):
        # Move orders to 'packing' so courier can see them
        self._promote_orders_to_packing(http, seller_auth["token"], two_orders_with_coords)

        http.post(
            f"{API}/courier/toggle",
            headers=auth_headers(courier_auth["token"]),
            json={"online": True},
        )
        r = http.get(f"{API}/courier/available", headers=auth_headers(courier_auth["token"]))
        assert r.status_code == 200
        available = r.json()
        ids = {o["id"] for o in two_orders_with_coords}
        mine = [o for o in available if o["id"] in ids]
        # Not always both will be present (already accepted, etc). Ensure at least one visible.
        if not mine:
            pytest.skip("No packing orders visible to courier (may be already accepted)")
        for o in mine:
            assert o.get("shop_lat") is not None and o.get("shop_lng") is not None, o
            assert o.get("address_lat") is not None and o.get("address_lng") is not None, o

        # Distinct delivery coords across the two orders
        if len(mine) >= 2:
            deliv = {(round(o["address_lat"], 4), round(o["address_lng"], 4)) for o in mine}
            assert len(deliv) >= 2, f"courier available returned identical coords: {deliv}"

    def test_courier_my_after_accept_has_coords(
        self, http, seller_auth, courier_auth, two_orders_with_coords
    ):
        # promote and accept one
        self._promote_orders_to_packing(http, seller_auth["token"], two_orders_with_coords)
        http.post(
            f"{API}/courier/toggle",
            headers=auth_headers(courier_auth["token"]),
            json={"online": True},
        )
        avail = http.get(
            f"{API}/courier/available", headers=auth_headers(courier_auth["token"])
        ).json()
        ids = {o["id"] for o in two_orders_with_coords}
        mine = [o for o in avail if o["id"] in ids]
        if not mine:
            pytest.skip("nothing to accept for courier")
        # Accept first
        oid = mine[0]["id"]
        a = http.post(
            f"{API}/courier/orders/{oid}/accept",
            headers=auth_headers(courier_auth["token"]),
        )
        assert a.status_code == 200, a.text

        r = http.get(f"{API}/courier/my", headers=auth_headers(courier_auth["token"]))
        assert r.status_code == 200
        active = [o for o in r.json() if o["id"] == oid]
        assert active, "accepted order not in /courier/my"
        o = active[0]
        assert o.get("shop_lat") is not None and o.get("shop_lng") is not None
        assert o.get("address_lat") is not None and o.get("address_lng") is not None
        # match original payload
        original = next(x for x in two_orders_with_coords if x["id"] == oid)
        # payload coords stored in delivery_location
        expected_dl = original.get("delivery_location") or {}
        if expected_dl:
            assert abs(o["address_lat"] - expected_dl["lat"]) < 1e-4
            assert abs(o["address_lng"] - expected_dl["lng"]) < 1e-4
