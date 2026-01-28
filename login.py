import json
import os
import logging
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

from config.settings import (
    FYERS_CLIENT_ID,
    FYERS_SECRET_KEY,
    FYERS_REDIRECT_URI,
    TOKEN_PATH,
    LOG_DIR,
    validate_config
)

# Setup logging
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'login.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def create_session():
    """Create a FYERS session model for authentication."""
    return fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code"
    )

def open_login_page(session):
    """Generate auth URL and open it in the browser."""
    auth_url = session.generate_authcode()
    logger.info("Opening browser for FYERS login...")
    webbrowser.open(auth_url)

def get_auth_code():
    """Parse auth code from the redirected URL."""
    redirected_url = input(
        "\nAfter login, paste the FULL redirected URL here:\n"
    )
    
    try:
        # Parse URL properly
        parsed_url = urlparse(redirected_url)
        query_params = parse_qs(parsed_url.query)
        
        if "auth_code" not in query_params:
            raise ValueError("auth_code not found in redirected URL")
        
        auth_code = query_params["auth_code"][0]
        logger.info("Successfully extracted auth code")
        return auth_code
        
    except Exception as e:
        logger.error(f"Failed to parse auth code: {e}")
        raise RuntimeError(f"Invalid redirected URL: {e}")

def generate_access_token(session, auth_code):
    """Generate access token from auth code."""
    session.set_token(auth_code)
    response = session.generate_token()
    
    if "access_token" not in response:
        logger.error("Failed to generate access token")
        raise RuntimeError("Failed to generate access token")
    
    logger.info("Access token generated successfully")
    return response["access_token"]

def save_token(access_token):
    """Save access token with expiration timestamp."""
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    
    # FYERS tokens typically expire in 24 hours
    expiration_time = datetime.now() + timedelta(hours=24)
    
    token_data = {
        "access_token": access_token,
        "expires_at": expiration_time.isoformat(),
        "created_at": datetime.now().isoformat()
    }
    
    with open(TOKEN_PATH, "w") as f:
        json.dump(token_data, f, indent=2)
    
    logger.info(f"Access token saved to {TOKEN_PATH}")
    logger.info(f"Token expires at: {expiration_time}")

def main():
    """Main login flow."""
    try:
        validate_config()
        logger.info("Starting FYERS login process")
        
        session = create_session()
        open_login_page(session)
        auth_code = get_auth_code()
        access_token = generate_access_token(session, auth_code)
        save_token(access_token)
        
        logger.info("Login completed successfully")
        print("\n✅ Login completed successfully.")
        
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise

if __name__ == "__main__":
    main()