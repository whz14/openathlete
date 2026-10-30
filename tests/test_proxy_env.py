from openathlete.proxy_env import normalize_no_proxy, normalize_proxy_env


def test_clean_no_proxy_is_byte_stable():
    value = "localhost, 127.0.0.1,::1,.example.com,::1/128,[::1]:8080,[not-an-ip]"
    assert normalize_no_proxy(value) == value


def test_bracketed_ipv6_entries_are_unwrapped_in_place():
    value = "localhost,127.0.0.1,::1,[::1], [2001:0db8::1] "
    assert normalize_no_proxy(value) == "localhost,127.0.0.1,::1,::1, 2001:0db8::1 "


def test_proxy_env_updates_both_no_proxy_names_only():
    env = {
        "NO_PROXY": "localhost,[::1]",
        "no_proxy": "127.0.0.1,[2001:db8::1]",
        "HTTP_PROXY": "http://proxy.example:8080",
    }
    normalize_proxy_env(env)
    assert env == {
        "NO_PROXY": "localhost,::1",
        "no_proxy": "127.0.0.1,2001:db8::1",
        "HTTP_PROXY": "http://proxy.example:8080",
    }
