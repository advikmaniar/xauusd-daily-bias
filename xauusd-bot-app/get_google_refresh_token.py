"""
Run this ONCE to get a Google OAuth refresh token for the app to read your
calendar going forward. It opens a browser window for you to approve access,
then prints the refresh token to save into your .env file.

Setup before running:
  1. Go to console.cloud.google.com -> create a project (or use an existing one)
  2. Enable the "Google Calendar API" for that project
  3. Go to APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
     - Application type: Desktop app
  4. Download the credentials JSON, save it as client_secret.json in this folder
  5. pip install google-auth-oauthlib --break-system-packages
  6. python get_google_refresh_token.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

print("\n--- Save these into your .env file ---")
print(f"GOOGLE_CLIENT_ID={creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
