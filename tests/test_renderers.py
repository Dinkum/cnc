from app.services.renderers import (
    render_follower_admin_proxy,
    render_nginx_http,
    render_nginx_shield_closed,
    render_nginx_static,
)


def test_render_nginx_http_contains_proxy_target() -> None:
    content = render_nginx_http("api.example.com", 12000)
    assert "server_name api.example.com;" in content
    assert "upstream cnc_upstream_api_example_com {" in content
    assert "server 127.0.0.1:12000;" in content
    assert "keepalive 64;" in content
    assert 'proxy_set_header Connection "";' in content
    assert "proxy_pass http://cnc_upstream_api_example_com;" in content


def test_render_nginx_static_contains_root() -> None:
    content = render_nginx_static("site.example.com", "/srv/site")
    assert "server_name site.example.com;" in content
    assert "root /srv/site;" in content


def test_render_nginx_http_cloudflare_acl_enabled() -> None:
    content = render_nginx_http(
        "api.example.com",
        12000,
        cloudflare_only=True,
        cloudflare_ips=("173.245.48.0/20", "2400:cb00::/32"),
    )
    assert "allow 127.0.0.1;" in content
    assert "allow ::1;" in content
    assert "allow 173.245.48.0/20;" in content
    assert "allow 2400:cb00::/32;" in content
    assert "deny all;" in content


def test_render_nginx_http_with_shield_auth_request() -> None:
    content = render_nginx_http(
        "api.example.com", 12000, shield_enabled=True, shield_output_key="app-one"
    )

    assert "upstream cnc_upstream_api_example_com_shield {" in content
    assert "server 127.0.0.1:1026;" in content
    assert "location = /shield/check {" in content
    assert "internal;" in content
    assert "proxy_pass http://cnc_upstream_api_example_com_shield/check;" in content
    assert "proxy_pass_request_body off;" in content
    assert "proxy_set_header X-Original-URI $request_uri;" in content
    assert "proxy_set_header X-Shield-Output app-one;" in content
    assert "location /shield/ {" in content
    assert "auth_request /shield/check;" in content
    assert "error_page 401 = @cnc_shield_login;" in content
    assert "return 302 /shield/access?next=$request_uri;" in content
    assert "proxy_pass http://cnc_upstream_api_example_com;" in content


def test_render_nginx_http_accepts_remote_shield_target() -> None:
    content = render_nginx_http(
        "api.example.com",
        "100.64.0.19:12000",
        shield_enabled=True,
        shield_target="100.64.0.10:1026",
    )

    assert "server 100.64.0.10:1026;" in content
    assert "server 127.0.0.1:1026;" not in content


def test_render_nginx_static_with_shield_auth_request() -> None:
    content = render_nginx_static(
        "site.example.com",
        "/srv/site",
        shield_enabled=True,
        shield_output_key="input:42",
    )

    assert "upstream cnc_upstream_site_example_com_shield {" in content
    assert "location = /shield/check {" in content
    assert "proxy_pass http://cnc_upstream_site_example_com_shield/check;" in content
    assert "proxy_set_header X-Shield-Output input:42;" in content
    assert "location /shield/ {" in content
    assert "auth_request /shield/check;" in content
    assert "try_files $uri $uri/ =404;" in content


def test_render_nginx_shield_closed_denies_direct_public_access() -> None:
    content = render_nginx_shield_closed("shield.example.com")

    assert "server_name shield.example.com;" in content
    assert "return 404;" in content
    assert "proxy_pass" not in content


def test_render_nginx_static_cloudflare_acl_disabled() -> None:
    content = render_nginx_static(
        "site.example.com",
        "/srv/site",
        cloudflare_only=False,
        cloudflare_ips=("173.245.48.0/20",),
    )
    assert "allow 173.245.48.0/20;" not in content
    assert "deny all;" not in content


def test_render_follower_admin_proxy_is_tailnet_restricted() -> None:
    content = render_follower_admin_proxy(
        "leader.example.ts.net",
        node_uid="node-a",
        server_names=("100.64.0.19", "node-a.example.ts.net"),
    )

    assert "listen 127.0.0.1:9091;" in content
    assert "server_name 100.64.0.19 node-a.example.ts.net;" in content
    assert "allow 100.64.0.0/10;" in content
    assert "deny all;" in content
    assert "proxy_pass https://leader.example.ts.net;" in content
    assert "proxy_set_header X-CNC-Follower-Node node-a;" in content
