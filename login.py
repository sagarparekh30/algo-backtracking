import json
import os
import webbrowser
from fyers_apiv3 import fyersModel

from config.settings import (
    FYERS_CLIENT_ID,
    FYERS_SECRET_KEY,
    FYERS_REDIRECT_URI,
    TOKEN_PATH
)

def sanity_check():
    if not FYERS_CLIENT_ID:
        raise RuntimeError("FYERS_CLIENT_ID not set in environment")

    if not FYERS_SECRET_KEY:
        raise RuntimeError("FYERS_SECRET_KEY not set in environment")

def create_session():
    return fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

def open_login_page(session):
    auth_url = session.generate_authcode()
    print("\nOpening browser for FYERS login...")
    webbrowser.open(auth_url)

def get_auth_code():
    redirected_url = input(
        "\nAfter login, paste the FULL redirected URL here:\n"
    )

    if "auth_code=" not in redirected_url:
        raise RuntimeError("auth_code not found in redirected URL")

    return redirected_url.split("auth_code=")[1].split("&")[0]

def generate_access_token(session, auth_code):
    session.set_token(auth_code)
    response = session.generate_token()

    if "access_token" not in response:
        raise RuntimeError("Failed to generate access token")

    return response["access_token"]

def save_token(access_token):
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)

    with open(TOKEN_PATH, "w") as f:
        json.dump({"access_token": access_token}, f, indent=2)

    print(f"\n✅ Access token saved to {TOKEN_PATH}")

def main():
    sanity_check()
    session = create_session()
    open_login_page(session)
    auth_code = get_auth_code()
    access_token = generate_access_token(session, auth_code)
    save_token(access_token)

    print("\n🎉 Login completed successfully.")

if __name__ == "__main__":
    main()