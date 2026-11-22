"""CI acceptance for two customers on a disposable loopback deployment."""
import argparse
import http.cookiejar
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPSHandler, Request


class Customer:
    def __init__(self, origin, context):
        self.origin = origin
        self.cookies = http.cookiejar.CookieJar()
        self.http = build_opener(HTTPSHandler(context=context), HTTPCookieProcessor(self.cookies))
        self.call('/auth/csrf')

    def call(self, path, *, method='GET', body=None, expect=200):
        headers = {'Accept': 'application/json'}
        if method != 'GET':
            headers.update({'Origin': self.origin, 'X-CSRFToken': next(c.value for c in self.cookies if c.name == 'csrftoken')})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = Request(self.origin + '/api/v1' + path, data=data, headers=headers, method=method)
        try:
            response = self.http.open(request, timeout=12)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == expect, f'{method} /{path.split("/")[1]} returned {response.status}, expected {expect}'
            raw = response.read()
            return json.loads(raw) if raw else None

    def signup(self, email):
        profile = self.call('/auth/register', method='POST', body={
            'email': email, 'password': secrets.token_urlsafe(28), 'display_name': 'Smoke customer', 'kitchen_name': 'Smoke kitchen',
        }, expect=201)
        assert profile['user']['email'] == email and profile['user']['email_verified'] is False
        org, = profile['organizations']
        assert org['role'] == 'admin'
        assert self.call('/auth/me')['user']['id'] == profile['user']['id']
        return org['id']


def verification_params(recipient, origin, local_mail):
    deadline = time.monotonic() + 45
    with tempfile.TemporaryDirectory() as tmp:
        mailbox = local_mail or Path(tmp)
        while time.monotonic() < deadline:
            if local_mail is None:
                subprocess.run(['docker', 'compose', 'cp', 'mail:/tmp/recipe-mail/.', str(mailbox)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            for path in mailbox.glob('*.log'):
                msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                if msg['To'] == recipient:
                    for line in msg.get_body().get_content().splitlines():
                        if line.startswith(origin + '/verify-email?'):
                            return {key: value[0] for key, value in parse_qs(urlsplit(line).query).items()}
            time.sleep(1)
    raise RuntimeError('Confirmation email did not arrive in the isolated test mailbox')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='https://localhost')
    parser.add_argument('--ca-file')
    parser.add_argument('--local-mail', type=Path)
    args = parser.parse_args()
    target = urlsplit(args.origin)
    if target.hostname not in {'localhost', '127.0.0.1'} or target.scheme not in {'http', 'https'} or target.path:
        parser.error('This test requires a disposable loopback deployment.')
    context = ssl.create_default_context(cafile=args.ca_file)
    owner, other = Customer(args.origin, context), Customer(args.origin, context)
    suffix = secrets.token_hex(7) + '@smoke.invalid'
    email_a, email_b = 'owner-' + suffix, 'guest-' + suffix
    kitchen_a, kitchen_b = owner.signup(email_a), other.signup(email_b)
    assert kitchen_a != kitchen_b
    base_a, base_b = '/orgs/' + kitchen_a, '/orgs/' + kitchen_b
    assert owner.call(base_a + '/billing')['plan'] == 'free'
    other.call(base_a + '/team', expect=404)
    owner.call(base_b + '/instructions', expect=404)
    owner.call('/operator/customers', expect=403)
    params = verification_params(email_a, args.origin, args.local_mail)
    owner.call('/auth/email/verify', method='POST', body=params)
    assert owner.call('/auth/me')['user']['email_verified']
    invitation = owner.call(base_a + '/invitations', method='POST', body={'email': email_b, 'role': 'reader'}, expect=201)
    accept = '/auth/invitations/' + invitation['invite_url'].rsplit('/', 1)[1]
    joined = other.call(accept, method='POST', body={})
    assert {org['id']: org['role'] for org in joined['organizations']} == {kitchen_b: 'admin', kitchen_a: 'reader'}
    other.call(base_a + '/team', expect=403)
    other.call(base_a + '/instructions')
    member = next(m for m in owner.call(base_a + '/team')['members'] if m['email'] == email_b)
    owner.call(base_a + '/team/' + member['id'], method='DELETE', expect=204)
    other.call(base_a + '/instructions', expect=404)
    assert other.call(base_b + '/billing')['plan'] == 'free'
    print('Signup, CSRF, sessions, delivery, tenant isolation, invitations, removal and free entitlement passed.')


if __name__ == '__main__':
    main()
