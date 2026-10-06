def test_strong_auth_helper_exists():
    from bank_lib.minebank_bonus import strong_auth
    assert callable(strong_auth)
