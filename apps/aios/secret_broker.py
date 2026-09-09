"""Fixed-destination credential use; raw credentials never leave this broker."""
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SecretBroker:
    def __init__(self, sessions, store):
        self.sessions, self.store = sessions, store

    def github_profile(self, token):
        self.sessions.use(token, 'secrets.github.profile', 'github')
        value = self.store.get('github-' + self.sessions.owner)
        if not value:
            raise PermissionError("No account configured")
        request = urllib.request.Request('https://api.github.com/user', headers={
            'Authorization': 'Bearer ' + value['token'], 'Accept': 'application/vnd.github+json',
            'User-Agent': 'AIOS-local-secret-broker'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
                result = json.loads(response.read(65536))
            # Recheck presence before releasing even the allowlisted response.
            self.sessions._present()
            return {'login': result['login']}
        except Exception:
            raise RuntimeError("Protected account request failed") from None
