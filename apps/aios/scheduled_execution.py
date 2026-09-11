"""Credential-free route bindings and budgets for unattended agent turns."""
from dataclasses import dataclass, field
import hashlib
import json
from types import SimpleNamespace

from . import core, principals, skills
from .scheduling import SchedulingError, UnavailableError


class NeedsUserAction(SchedulingError):
    code = 'needs_user_action'


def desktop_only():
    if principals.current() is not None:
        raise UnavailableError('Scheduling is unavailable in protected workspaces')


def _route(profile, config):
    if profile == 'current':
        mode = config['mode']
        if mode == 'local':
            from .local_runtime import _model_identity
            return 'local', 'local', _model_identity(config.get('model_path', ''))
        if mode == 'remote':
            return 'remote', config['model'], core.validate_url(config['url'])
        if mode == 'chatgpt':
            return 'subscription', config.get('subscription_model', ''), 'chatgpt'
    elif profile == 'agent':
        if config.get('agent_mode') == 'remote':
            url, model, _ = core._remote_agent_settings(config)
            return 'remote', model, url
        if config.get('agent_mode') == 'chatgpt':
            return 'subscription', config.get('subscription_model', ''), 'chatgpt'
    raise NeedsUserAction('The saved provider profile is no longer configured')


def _reference(profile, provider, model, destination):
    encoded = json.dumps([profile, provider, model, destination], separators=(',', ':')).encode()
    return profile + '@' + hashlib.sha256(encoded).hexdigest()


def bind(execution, *, require_credentials=False):
    desktop_only()
    config = core.load_config()
    profile = execution['profile'].split('@', 1)[0]
    try:
        provider, model, destination = _route(profile, config)
    except (KeyError, ValueError, OSError, RuntimeError):
        raise NeedsUserAction('Review the saved provider and model in AI models settings') from None
    reference = _reference(profile, provider, model, destination)
    if (execution['provider'] != provider or execution['model'] != model
            or execution['profile'] not in (profile, reference)):
        raise NeedsUserAction('The saved provider or model changed; review and save the job again')
    if not model or (provider == 'local' and not destination):
        raise NeedsUserAction('Choose an explicit installed model before scheduling this job')
    if require_credentials:
        if provider == 'remote':
            key = config.get('agent_api_key' if profile == 'agent' else 'api_key')
            try:
                core._validate_agent_api_key(key, 'Invalid saved provider API key')
            except ValueError:
                raise NeedsUserAction('Configure the saved provider API key, then resume this job') from None
            if not key.strip():
                raise NeedsUserAction('Configure the saved provider API key, then resume this job')
        if provider == 'subscription':
            from .subscription import private_home
            if not (private_home() / 'auth.json').is_file():
                raise NeedsUserAction('Sign in with ChatGPT in AI models settings, then resume this job')
    return {**execution, 'profile': reference}, config


def binding(prompt):
    """Resolve the existing initial-skill routing without starting tools."""
    from .agent import select_provider
    desktop_only()
    catalog = skills.load_skills()
    active = skills.initial_skills(catalog, prompt)
    preferred = any(skill.model == 'remote-preferred' for skill in active)
    config = core.load_config()
    provider, profile = select_provider(SimpleNamespace(remote_preferred=preferred), config)
    if provider == 'chatgpt':
        profile = 'agent' if preferred and config.get('agent_mode') == 'chatgpt' else 'current'
    try:
        provider, model, destination = _route(profile, config)
    except (KeyError, ValueError, OSError, RuntimeError):
        raise NeedsUserAction('Review the saved provider and model in AI models settings') from None
    if not model:
        raise NeedsUserAction('Choose an explicit model in AI models settings')
    return {'provider': provider, 'profile': _reference(profile, provider, model, destination),
            'model': model}


def permitted_capabilities():
    """Only explicitly named, currently allowlisted MCP tools run unattended."""
    from .mcp import _load_servers, _exposed_name
    names = ['browser', 'application', 'os_settings']
    servers, warnings = _load_servers(None)
    for server, settings in servers:
        for tool in settings['tools']:
            if tool == '*':
                warnings.append('Background MCP access requires explicit tool names, not a wildcard.')
                continue
            if tool in ('scheduled_jobs', 'schedule_job', 'authenticate', 'authentication_status'):
                continue
            exposed = _exposed_name(server, tool)
            if exposed and exposed not in names:
                names.append(exposed)
    return {'capabilities': names, 'warnings': warnings}


@dataclass
class BackgroundContext:
    execution: dict
    output_bytes: int = 0
    tool_calls: int = 0
    usage: dict = field(default_factory=dict)

    def configuration(self):
        _, config = bind(self.execution, require_credentials=True)
        return config

    @property
    def profile(self):
        return self.execution['profile'].split('@', 1)[0]

    @property
    def remaining_tokens(self):
        return self.execution['token_budget'] - max(self.output_bytes, self.usage.get('output_tokens', 0))

    def output(self, value):
        # A UTF-8 byte is a conservative output-token unit, independent of a
        # provider's tokenizer. Count tool arguments as well as visible prose.
        self.output_bytes += len(value.encode('utf-8'))
        if self.output_bytes > min(65536, self.execution['token_budget']):
            raise RuntimeError('Scheduled run output budget reached')

    def tool(self):
        if self.tool_calls >= self.execution['tool_budget']:
            raise RuntimeError('Scheduled run tool budget reached')
        self.tool_calls += 1

    def reported_usage(self, value):
        for source, target in (('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')):
            if source not in value:
                continue
            count = value[source]
            if type(count) is not int or not 0 <= count <= 2 ** 30:
                raise RuntimeError('The provider returned invalid token usage')
            self.usage[target] = self.usage.get(target, 0) + count
        if self.usage.get('output_tokens', 0) > self.execution['token_budget']:
            raise RuntimeError('Scheduled run output budget reached')
