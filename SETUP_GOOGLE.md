# Setting up Google Sheets access (one-time, about 10 minutes)

Breakdown Studio can build and update a live Google Sheet with your shot breakdown. To do that, it needs its own OAuth credential, one that you create, that signs in as *your* Google account. There is no shared bot and no third-party server in the middle: your footage, frames, and sheet data never leave your machine and your own Google account.

You only do this once per Google account. Every future film on that account reuses the same credential.

## What you're about to do

Create a small, free "app" inside your own Google Cloud project (it's really just a name and a client ID), turn on two APIs for it, and download a JSON file that Breakdown Studio uses to ask your Google account for permission to read and write your sheets and Drive.

## Steps

1. **Open the Google Cloud Console.**
   Go to [console.cloud.google.com](https://console.cloud.google.com/) and sign in with the Google account that owns (or will own) your breakdown sheets.

2. **Create a project.**
   - Click the project dropdown at the top of the page, then **New Project**.
   - Give it any name (for example, "Breakdown Studio"). It doesn't need to match anything else.
   - Click **Create**, then wait a few seconds and select the new project from the dropdown.

3. **Enable the two APIs you need.**
   - In the left sidebar (or the top search bar), go to **APIs & Services** -> **Library**.
   - Search for **Google Sheets API**, open it, and click **Enable**.
   - Search for **Google Drive API**, open it, and click **Enable**.
   - (Sheets lets the app read and write your breakdown. Drive lets it create new sheets from the template and manage thumbnail images.)

4. **Configure the OAuth consent screen.**
   - Go to **APIs & Services** -> **OAuth consent screen**.
   - Choose **External** as the user type (this is correct even though only you will use it), then **Create**.
   - Fill in the required fields: app name (anything, e.g. "Breakdown Studio"), your email as the support email, and your email again as the developer contact.
   - On the **Scopes** step, you can skip adding scopes here; the app requests what it needs when you connect.
   - On the **Test users** step, click **Add users** and add your own Google account's email address. This is important: without this, Google will refuse to let you sign in.
   - Save through to the end of the wizard.

5. **Create the OAuth client credential.**
   - Go to **APIs & Services** -> **Credentials**.
   - Click **Create credentials** -> **OAuth client ID**.
   - For **Application type**, choose **Desktop app**.
   - Give it any name (for example, "Breakdown Studio Desktop").
   - Click **Create**.

6. **Download the JSON.**
   - After creation, click the download icon next to the new client (or find it in the credentials list and click it, then **Download JSON**).
   - Save the file somewhere sensible, for example next to your `breakdown_studio` folder. Do not commit it to a public repository; it identifies your OAuth client (though it is useless without your own account's consent).

7. **Point Breakdown Studio at it.**
   - Open Breakdown Studio, click **Settings…**.
   - Set **Google OAuth client secret JSON** to the file you just downloaded.
   - Leave **Google token cache** blank; it defaults to `.gtoken.json` beside the app and is created automatically the first time you connect.
   - Click **Save**.

8. **Connect and approve.**
   - Back in the main window, click **Connect Google…**.
   - A browser window opens asking you to sign in and approve access. Sign in with the same account you added as a test user in step 4, and approve.
   - You're done. The app caches a token so you won't have to sign in again for a while.

## Common snags

**"Google hasn't verified this app" warning.**
This is expected and safe. Because the app is unverified (verification is a lengthy process meant for public-facing apps with many users), Google shows a warning screen. Click **Advanced**, then click **Go to [your app name] (unsafe)**. This is *your own app*, created in step 5, signing in as *your own account*. There is nothing unsafe about it; the warning is generic boilerplate for any unverified OAuth client.

**Sign-in works, but breaks again after about a week.**
Test-mode OAuth consent screens issue tokens that expire after 7 days. You have two options:
- Just re-consent when prompted (click **Connect Google…** again); it takes a few seconds.
- Or avoid it entirely: go back to **OAuth consent screen** in the Cloud Console and click **Publish App** to move it out of Testing into Production. For a personal-use app requesting only Sheets/Drive scopes on your own account, this does not require Google's verification review; it just removes the 7-day token expiry.

**Signed in with the wrong Google account.**
Make sure you're signing in with the account that owns (or should own) your breakdown sheets, not whichever account happens to be logged into your browser. If you have multiple Google accounts, use an incognito/private window for the **Connect Google…** step, or sign out of the wrong account first.

**Privacy note.**
The OAuth client you created is yours alone. Nobody else has access to it, and Breakdown Studio does not route your data through any third-party server. Your footage stays local; only sheet cell data and thumbnail images (which you are already choosing to put in your own Google Sheet) go to Google, via your own account, the same as if you'd typed them in by hand.
