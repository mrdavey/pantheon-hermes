import contextlib
import contextvars
import importlib.util
import json
import base64
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from ruamel.yaml import YAML
yaml = YAML(typ="safe")
yaml.default_flow_style = False
from unittest.mock import patch

# Exported root names are chosen by the native installer, not by this suite.
if 'hermes' not in sys.modules:
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('hermes',root/'__init__.py',submodule_search_locations=[str(root)])
    module=importlib.util.module_from_spec(spec);sys.modules['hermes']=module;spec.loader.exec_module(module)
from hermes import register
from hermes.pantheon_native import api, files, state, permissions
from hermes.pantheon_native.runtime import Runtime
from hermes.pantheon_native.session import observe
from hermes.pantheon_native.tools import Tools, CATALOG, invitation

PROFILE = contextvars.ContextVar('test_profile', default='default')
SESSION = {'id':'session-1','route':'session-1','profile':'default','surface':'desktop'}
CONNECTION = 'connection_01234567890'
GRANT = 'grant_01234567890123456'


class Context:
    @property
    def profile_name(self): return PROFILE.get()
    def __init__(self): self.tools={};self.hooks={};self.skills={};self.unload=[];self.injected=[]
    def session_surface(self): return 'desktop'
    def session_cwd(self): return str(self.cwd)
    def register_skill(self, **kwargs): self.skills[kwargs['name']]=kwargs
    def register_tool(self, **kwargs): self.tools[kwargs['name']]=kwargs
    def register_hook(self, name, handler): self.hooks[name]=handler
    def on_unload(self, handler): self.unload.append(handler)
    def inject_message(self, content, *, session_key):
        self.injected.append((PROFILE.get(),content,session_key));return True


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        @contextlib.contextmanager
        def database():
            db=sqlite3.connect(Path(self.temp.name)/(PROFILE.get()+'.sqlite'),timeout=5)
            db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT,id TEXT,body TEXT NOT NULL,PRIMARY KEY(kind,id))');db.commit()
            try: yield db
            finally: db.close()
        self.patch=patch.object(state,'database',database);self.patch.start();self.addCleanup(self.patch.stop)
        self.ctx=Context();self.runtime=Runtime(self.ctx);self.addCleanup(self.runtime.stop)
        self.root=Path(self.temp.name).resolve()
        self.ctx.cwd=self.root/'workspace';self.ctx.cwd.mkdir()
        self.downloads=self.root/'downloads';self.downloads.mkdir()
        for name,value in [('download_root',self.downloads),('hermes_home',self.root/'profile')]:
            p=patch.object(files,name,return_value=value);p.start();self.addCleanup(p.stop)
        self.tools=Tools(self.ctx,self.runtime)
    def row(self, **extra):
        value={'id':CONNECTION,'chat':'room','member':'hermes','url':'https://pantheon.talk/app?chat=room',
               'deadline':'2099-01-01T00:00:00Z','state':'connected','session':dict(SESSION),'grant':GRANT}
        value.update(extra);state.put('connection',CONNECTION,value);return value
    def test_reopen_catches_up_without_join_or_read_ack(self):
        self.row(deadline='0001-01-01T00:00:00Z', runtime_policy={'mode':'connection'})
        notified=threading.Event()
        original=self.ctx.inject_message
        def inject(*args, **kwargs):
            result=original(*args, **kwargs);self.runtime.stop();notified.set();return result
        pending=[{'message':{'id':'pending-1','kind':'post'}}]
        def tool(connection, session, name, args):
            self.assertEqual(connection,CONNECTION);self.assertEqual(session,SESSION)
            self.assertIn(name,{'pantheon_chat_state','pantheon_receive'})
            return pending if name=='pantheon_receive' else {'state':'active'}
        with patch.object(permissions,'current',return_value=True), patch.object(api,'request',return_value={'grant_id':GRANT}) as request, patch.object(api,'tool',side_effect=tool), patch.object(self.ctx,'inject_message',side_effect=inject):
            self.runtime.reopen(SESSION)
            self.assertTrue(notified.wait(2))
            request.assert_called_once_with('GET','/identity')
        self.assertEqual(len(self.ctx.injected),1)
        self.assertNotIn('pending-1',self.ctx.injected[0][1])
        self.assertEqual(state.get('connection',CONNECTION)['runtime_policy'],{'mode':'connection'})

    def test_open_hook_restores_only_its_observed_conversation(self):
        register(self.ctx)
        with patch('hermes.observe',return_value=SESSION), patch.object(Runtime,'schedule_reopen') as reopen:
            self.ctx.hooks['on_session_open'](session_id='native-session',reason='resume')
            reopen.assert_called_once_with(SESSION)

    def test_reopen_does_not_change_grant_permission_or_receipts(self):
        original=self.row(wake='old',wake_state='attempted')
        with patch.object(permissions,'current',return_value=True), patch.object(api,'request',return_value={'grant_id':'another_grant_123456'}), patch.object(api,'tool') as tool, patch.object(self.runtime,'start') as start:
            self.runtime.reopen(SESSION);tool.assert_not_called();start.assert_not_called()
        self.assertEqual(state.get('connection',CONNECTION),original)
        with patch.object(permissions,'current',return_value=False),patch.object(api,'request') as request:
            self.runtime.reopen(SESSION);request.assert_not_called()
        with patch.object(permissions,'current',return_value=True),patch.object(api,'request') as request:
            self.runtime.reopen(dict(SESSION,id='another'));request.assert_not_called()

    def test_reopen_checks_revocation_and_preserves_finite_expiry(self):
        from hermes.pantheon_native.runtime import deadline
        original=self.row(deadline='2000-01-01T00:00:00Z')
        with patch.object(permissions,'current',return_value=True),patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(self.runtime,'disconnect') as disconnect,patch.object(self.runtime,'start') as start:
            self.runtime.reopen(SESSION);disconnect.assert_called_once_with(original);start.assert_not_called()
        self.row()
        with patch.object(permissions,'current',return_value=True),patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool',side_effect=api.APIError('access_unavailable')),patch.object(self.runtime,'start') as start:
            self.runtime.reopen(SESSION);start.assert_not_called()
        with self.assertRaises(api.APIError):deadline(dict(original,runtime_policy={'mode':'connection'}))

    def test_new_connection_lifetime_and_legacy_pending_intent_do_not_mix(self):
        secret='A'*43
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                with state.database() as db:
                    db.execute('DELETE FROM records');db.commit()
                if legacy:
                    intent=state.intent('join',SESSION,GRANT,state.digest([hashlib.sha256(secret.encode()).hexdigest(),'']))
                captured=[]
                def request(method,path,body=None):
                    if path=='/identity':return {'grant_id':GRANT}
                    if path.startswith('/operations/'):raise api.APIError('not_accepted')
                    self.assertEqual(path,'/connections/join');captured.append(body)
                    result=self.row()
                    if 'runtime_policy' in body:result.update(runtime_policy=body['runtime_policy'],deadline='0001-01-01T00:00:00Z')
                    else:result['deadline']=body['deadline']
                    return result
                with patch.object(api,'request',side_effect=request),patch.object(permissions,'ready',return_value=True),patch.object(self.runtime,'start'):
                    result=self.tools.dispatch('pantheon_connect',{'link':'https://pantheon.talk/join#invite='+secret,'deliberate':True},SESSION)
                if legacy:
                    self.assertNotIn('runtime_policy',captured[0]);self.assertNotIn('runtime_policy',result)
                else:self.assertEqual(captured[0]['runtime_policy'],{'mode':'connection'})

    def test_reopen_retries_transient_failure_and_stops_after_success(self):
        self.row()
        finished=threading.Event()
        original=self.runtime.reopen
        attempts=[]
        def attempt(session):
            attempts.append(session)
            result=original(session)
            if result is not False:finished.set()
            return result
        with patch.object(permissions,'current',return_value=True), patch.object(api,'request',side_effect=[api.APIError('outcome_unknown'),{'grant_id':GRANT}]), patch.object(api,'tool'), patch.object(self.runtime,'start') as start, patch.object(self.runtime,'reopen',side_effect=attempt):
            self.runtime.schedule_reopen(SESSION)
            self.assertTrue(finished.wait(4))
            self.runtime.stop()
            self.assertEqual(attempts,[SESSION,SESSION])
            start.assert_called_once()

    def test_disconnect_during_live_resume_check_wins(self):
        self.row()
        def checked(*args):state.cleanup_intent(CONNECTION)
        with patch.object(permissions,'current',return_value=True),patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool',side_effect=checked),patch.object(self.runtime,'start') as start:
            self.runtime.reopen(SESSION)
            start.assert_not_called()
        self.assertEqual(state.get('connection',CONNECTION)['state'],'cleanup_pending')

    def test_terminal_cleanup_deadline_never_moves_on_reopen(self):
        self.row(deadline='0001-01-01T00:00:00Z',runtime_policy={'mode':'connection'})
        with patch.object(state.time,'time',return_value=100):first=state.terminal_window(CONNECTION)
        with patch.object(state.time,'time',return_value=140):second=state.terminal_window(CONNECTION)
        self.assertEqual(first['terminal_deadline'],160)
        self.assertEqual(second['terminal_deadline'],160)

    def test_registration_inert_and_exact(self):
        with patch.object(state,'database',side_effect=AssertionError('storage at registration')),patch.object(api,'request',side_effect=AssertionError('network at registration')),patch.object(threading.Thread,'start',side_effect=AssertionError('thread at registration')):
            register(self.ctx)
        manifest=yaml.load((Path(__file__).parents[1]/'plugin.yaml').read_text())
        self.assertEqual(set(manifest['provides_tools']),set(self.ctx.tools))
        self.assertEqual(set(manifest['provides_hooks']),set(self.ctx.hooks))
        self.assertEqual(set(self.ctx.skills), {'collaborate'})
        self.assertEqual(self.ctx.skills['collaborate']['path'].read_text(),
                         (Path(__file__).parents[1]/'skills/collaborate/SKILL.md').read_text())
        for item in self.ctx.tools.values():
            self.assertFalse({'session','profile','token','authorization'} & set(item['schema']['parameters']['properties']))
    def test_invitation_rejects_untrusted_routes(self):
        secret='A'*43
        self.assertEqual(invitation('https://pantheon.talk/join?invite='+secret),secret)
        self.assertEqual(invitation('https://pantheon.talk/join#invite='+secret),secret)
        for link in ['https://pantheon.talk.evil/join?invite='+secret,'https://pantheon.talk/join?invite='+secret+'&invite='+secret,'https://pantheon.talk/join?invite='+secret+'#x','http://pantheon.talk/join?invite='+secret]:
            with self.assertRaises(api.APIError): invitation(link)
    def test_profiles_and_atomic_nonsecret_intents(self):
        found=[]
        def prepare(): found.append(state.intent('join',SESSION,GRANT,'digest'))
        workers=[threading.Thread(target=prepare) for _ in range(4)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual(len({x['operation'] for x in found}),1)
        token=PROFILE.set('other')
        try:self.assertEqual(state.rows('intent'),[])
        finally:PROFILE.reset(token)
        self.assertNotIn('invitation',json.dumps(found))
    def test_lost_join_recovers_without_resending_invitation(self):
        calls=[]; saved={}
        def request(method,path,body=None):
            calls.append((method,path,body))
            if path=='/identity':return {'grant_id':GRANT}
            if path.startswith('/operations/'):
                if saved:return saved
                raise api.APIError('not_accepted',404)
            if path=='/connections/join':
                saved.update(self.row());saved.pop('grant');saved['session']=SESSION['id']
                raise api.APIError('outcome_unknown')
            if path=='/connections/'+CONNECTION:return saved
            if path=='/connections':return {'connections':[saved]}
            raise AssertionError(path)
        args={'link':'https://pantheon.talk/join?invite='+'A'*43,'deliberate':True}
        with patch.object(api,'request',request),patch.object(permissions,'ready',return_value=True),patch.object(self.runtime,'start'):
            with self.assertRaises(api.APIError):self.tools.dispatch('pantheon_connect',args,SESSION)
            result=self.tools.dispatch('pantheon_status',{},SESSION)
        self.assertEqual(result['connections'][0]['id'],CONNECTION)
        self.assertEqual(sum(path=='/connections/join' for _,path,_ in calls),1)
        self.assertNotIn('A'*43,json.dumps(state.rows('intent')+state.rows('connection')))
    def test_session_and_grant_fences_precede_tools(self):
        self.row()
        with patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool') as tool:
            with self.assertRaises(api.APIError):self.tools.dispatch('pantheon_post',{'connection':CONNECTION,'key':'op','text':'no'},dict(SESSION,id='other'))
            self.assertFalse(tool.called)
        with patch.object(api,'request',return_value={'grant_id':'new_grant_012345678901234'}),patch.object(api,'tool') as tool:
            with self.assertRaises(api.APIError):self.tools.dispatch('pantheon_receive',{'connection':CONNECTION},SESSION)
            self.assertFalse(tool.called)
    def test_read_never_acks_and_ready_uses_current_preconditions(self):
        self.row();calls=[]
        def tool(connection,session,name,args):
            calls.append((name,args))
            if name=='pantheon_chat_state':return {'state':'active','epoch':3,'config':7}
            return [] if name=='pantheon_receive' else {'status':'ok'}
        with patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool',tool),patch.object(self.runtime,'start'):
            self.tools.dispatch('pantheon_receive',{'connection':CONNECTION},SESSION)
            self.assertEqual([name for name,_ in calls],['pantheon_receive'])
            self.tools.dispatch('pantheon_ack_ready',{'connection':CONNECTION,'key':'read','message_ids':['probe'],'text':'probe','objective_version':2,'context_version':3},SESSION)
        self.assertEqual(calls[-1][1]['epoch'],3);self.assertEqual(calls[-1][1]['config'],7)
    def test_wake_claim_is_bounded_and_uncertain_not_replayed(self):
        self.row()
        self.assertTrue(state.claim_wake(CONNECTION,'first'))
        self.assertFalse(state.claim_wake(CONNECTION,'second'))
        state.update('connection',CONNECTION,{'wake_state':'consumed'})
        self.assertFalse(state.claim_wake(CONNECTION,'first'))
        self.assertTrue(state.claim_wake(CONNECTION,'second'))
    def test_receiver_captures_profile_and_stops_on_unload(self):
        token=PROFILE.set('named')
        try:
            row=self.row(session=dict(SESSION,profile='named'))
            with patch.object(permissions,'current',return_value=True),patch.object(api,'tool',return_value=[{'message':{'id':'m'}}]):
                self.runtime.start(row)
                limit=time.monotonic()+2
                while not self.ctx.injected and time.monotonic()<limit:time.sleep(.01)
                self.runtime.stop()
            self.assertEqual(self.ctx.injected[0][0],'named')
            self.assertIn(row['url'],self.ctx.injected[0][1])
            self.assertNotIn('peer content',self.ctx.injected[0][1])
        finally:PROFILE.reset(token)
    def test_cleanup_reuses_operation_after_network_loss(self):
        row=self.row();calls=[]
        def request(method,path,body=None):
            calls.append(body['operation'])
            if len(calls)==1:raise api.APIError('outcome_unknown')
            return {'state':'disconnected'}
        with patch.object(api,'request',request),patch.object(permissions,'restore'):
            self.runtime.disconnect(row);self.runtime.disconnect(state.get('connection',CONNECTION))
        self.assertEqual(calls[0],calls[1]);self.assertEqual(state.get('connection',CONNECTION)['state'],'disconnected')
    def test_metadata_only_session_rejects_delegate_before_lineage(self):
        calls=[]
        class DB:
            def __init__(self,read_only):self.read_only=read_only
            def get_session_list_row(self,session):return {'source':'subagent'}
            def get_conversation_root(self,session):calls.append('lineage');return 'parent'
            def close(self):calls.append('close')
        with patch.dict(sys.modules,{'gateway':types.ModuleType('gateway'),'gateway.session_context':types.SimpleNamespace(get_session_env=lambda name:'s'),'hermes_state':types.SimpleNamespace(SessionDB=DB)}):
            with self.assertRaises(api.APIError):observe(self.ctx,'child')
        self.assertEqual(calls,['close'])
    def test_desktop_storage_normalization_preserves_connection_binding(self):
        row = {'source': 'gui', 'hidden': 0, 'archived': 0}
        class DB:
            def __init__(self, read_only): assert read_only
            def get_session_list_row(self, session): return dict(row)
            def get_conversation_root(self, session): return 'conversation-root'
            def close(self): pass
        modules = {'gateway': types.ModuleType('gateway'),
                   'gateway.session_context': types.SimpleNamespace(get_session_env=lambda name: 'native-route'),
                   'hermes_state': types.SimpleNamespace(SessionDB=DB)}
        with patch.dict(sys.modules, modules):
            initial = observe(self.ctx, 'native-session')
            row['source'] = 'local'
            self.assertEqual(observe(self.ctx, 'native-session'), initial)
        self.assertEqual(initial, {'id': 'conversation-root', 'route': 'native-route',
                                   'profile': 'default', 'surface': 'desktop'})
    def test_local_storage_does_not_admit_unsupported_or_hidden_sessions(self):
        row = {'source': 'local', 'hidden': 0, 'archived': 0}
        class DB:
            def __init__(self, read_only): assert read_only
            def get_session_list_row(self, session): return dict(row)
            def get_conversation_root(self, session): raise AssertionError('lineage before admission')
            def close(self): pass
        modules = {'gateway': types.ModuleType('gateway'),
                   'gateway.session_context': types.SimpleNamespace(get_session_env=lambda name: 'native-route'),
                   'hermes_state': types.SimpleNamespace(SessionDB=DB)}
        with patch.dict(sys.modules, modules):
            for surface in ('acp', 'telegram', '', 'unknown'):
                with self.subTest(surface=surface), patch.object(self.ctx, 'session_surface', return_value=surface):
                    with self.assertRaises(api.APIError): observe(self.ctx, 'native-session')
            for source in ('subagent', 'tool', 'oneshot', 'cron', 'bot_room'):
                row['source'] = source
                with self.subTest(source=source), self.assertRaises(api.APIError): observe(self.ctx, 'native-session')
            row['source'] = 'gui'
            for flag in ('hidden', 'archived'):
                row[flag] = 1
                with self.subTest(flag=flag), self.assertRaises(api.APIError): observe(self.ctx, 'native-session')
                row[flag] = 0
    def test_real_host_desktop_metadata_when_available(self):
        try:
            from hermes_state import SessionDB
            from gateway.session_context import scoped_session_key
        except ImportError:
            self.skipTest('real Hermes host supplied by the isolated contract runner')
        # The contract runner supplies a disposable profile. Never use owner state.
        import os
        with patch.dict(os.environ, {'HERMES_HOME': self.temp.name}):
            db = SessionDB()
            try: db.create_session('native-desktop-fixture', source='gui')
            finally: db.close()
            with scoped_session_key('native-desktop-route'):
                result = observe(self.ctx, 'native-desktop-fixture')
        self.assertEqual(result['surface'], 'desktop')
        self.assertEqual(result['id'], 'native-desktop-fixture')
        self.assertEqual(result['route'], 'native-desktop-route')
        # A reopen occurs outside a model turn, before ctx.session_surface has
        # an active policy. Only the host's hook platform may fill that gap.
        register(self.ctx)
        with patch.dict(os.environ, {'HERMES_HOME': self.temp.name}), scoped_session_key('native-desktop-route'), patch.object(self.ctx,'session_surface',return_value=''), patch.object(Runtime,'schedule_reopen') as reopen:
            self.ctx.hooks['on_session_open'](session_id='native-desktop-fixture',platform='desktop',reason='resume')
            reopen.assert_called_once_with(result)
    def test_download_integrity_no_overwrite_and_explicit_receipt(self):
        import base64,hashlib
        target=self.downloads/'saved.txt';data=b'complete payload'
        meta={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        receipt=files.save_file(str(target),base64.b64encode(data).decode(),meta)
        self.assertTrue(receipt['durable']);self.assertEqual(target.read_bytes(),data)
        self.assertEqual(files.save_file(str(target),base64.b64encode(data).decode(),meta),receipt)
        target.write_text('changed')
        with self.assertRaises(api.APIError):files.save_file(str(target),base64.b64encode(data).decode(),meta)
        with self.assertRaises(api.APIError):files.save_file(str(target),'eA==',meta)
    def test_error_redaction_and_no_authority_arguments(self):
        with patch('hermes.pantheon_native.tools.observe',return_value=SESSION),patch.object(api,'request',side_effect=RuntimeError('secret access_token=PRIVATE')):
            result=self.tools.call('pantheon_doctor',{})
            self.assertNotIn('PRIVATE',result)
            self.assertIn('invalid_arguments',self.tools.call('pantheon_doctor',{'session':'forged'}))

    def test_auth_recovery_preflights_config_before_human_login(self):
        # Fresh unsigned hosts can hide the missing ID behind sign_in_required.
        for code in ('sign_in_required', 'native_auth_unavailable', 'public_client_id_required'):
            with self.subTest(code=code), patch('hermes.pantheon_native.tools.observe',return_value=SESSION), patch.object(api,'request',side_effect=api.APIError(code)):
                result=json.loads(self.tools.call('pantheon_doctor',{}))
            self.assertEqual(result['error'],code)
            self.assertNotIn('setup_confirmation',result)
            instruction=result['instruction']
            self.assertLess(instruction.index('hermes config get'),instruction.index('hermes config set'))
            self.assertLess(instruction.index('readback in a fresh command'),instruction.index('hermes auth add'))
            for required in ('Preserve a different configured value', 'other config failures stop setup',
                             '--profile <observed-profile>', 'human-owned', 'env -u PANTHEON_OAUTH_CLIENT_ID'):
                self.assertIn(required,instruction)

    def test_setup_confirmation_requires_live_connection_and_permission(self):
        for status in ('connected', 'waiting', 'disconnected'):
            self.row(state=status)
            def request(method,path,body=None):
                if path=='/identity':return {'grant_id':GRANT}
                if path.startswith('/operations/'):raise api.APIError('not_accepted',404)
                if path.endswith('/resume'):return self.row(state=status)
                raise AssertionError(path)
            with self.subTest(status=status), patch.object(api,'request',request), patch.object(permissions,'ready',return_value=True), patch.object(self.runtime,'start'):
                result=self.tools.dispatch('pantheon_resume',{'connection':CONNECTION,'deliberate':True},SESSION)
            if status in ('connected','waiting'):
                self.assertEqual(result['setup_confirmation'],r'\o/ Pantheon setup complete!')
                self.assertIn('show setup_confirmation once',result['next'])
                self.assertIn('already-ready installation or retry',result['next'])
            else:self.assertNotIn('setup_confirmation',result)
        with patch.object(api,'request',return_value={'grant_id':GRANT}), patch.object(permissions,'ready',return_value=False), patch.object(self.runtime,'start') as start:
            result=self.tools.dispatch('pantheon_resume',{'connection':CONNECTION,'deliberate':True},SESSION)
        self.assertEqual(result['error'],'incoming_permission_required')
        self.assertNotIn('setup_confirmation',result)
        start.assert_not_called()

    def test_permission_restoration_does_not_override_later_human_choice(self):
        state.put('permission','prior',{'value':None})
        removed=[]
        config=types.SimpleNamespace(unset_config_value=lambda key:removed.append(key),set_config_value=lambda key,value:removed.append((key,value)))
        with patch.dict(sys.modules,{'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.config':config}),patch.object(permissions,'current',return_value=True):
            permissions.restore()
            self.assertIsNone(state.get('permission','prior'))
            # A later, independently enabled setting has no Pantheon baseline.
            self.assertTrue(permissions.ready());permissions.restore()
        self.assertEqual(removed,[permissions.KEY])

    def test_auth_loss_restores_permission_after_last_receiver(self):
        row=self.row()
        state.put('permission','prior',{'value':None})
        removed=[]
        config=types.SimpleNamespace(unset_config_value=lambda key:removed.append(key),set_config_value=lambda key,value:removed.append((key,value)))
        with patch.dict(sys.modules,{'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.config':config}),patch.object(permissions,'current',return_value=True),patch.object(api,'tool',side_effect=api.APIError('sign_in_required')),patch.object(api,'request',side_effect=api.APIError('sign_in_required')):
            self.runtime.run(row,threading.Event())
        self.assertEqual(state.get('connection',row['id'])['state'],'authorization_unavailable')
        self.assertEqual(removed,[permissions.KEY])
        # Loss of this grant must not restore while another connection is active.
        state.put('permission','prior',{'value':False})
        self.row()
        permissions.restore()
        self.assertIsNotNone(state.get('permission','prior'))

    def test_desktop_disconnect_stops_receiver_and_restores_permission(self):
        row=self.row()
        with patch.object(permissions,'current',return_value=True),patch.object(permissions,'restore') as restore,patch.object(api,'tool',side_effect=api.APIError('sign_in_required')),patch.object(api,'request',return_value={'state':'disconnected'}) as request:
            self.runtime.run(row,threading.Event())
        self.assertEqual(state.get('connection',CONNECTION)['state'],'disconnected')
        request.assert_called_once_with('GET','/connections/'+CONNECTION)
        restore.assert_called_once()
        self.assertEqual(self.ctx.injected,[])

    def test_authorization_loss_does_not_restart_on_next_turn(self):
        row=self.row(state='authorization_unavailable')
        with patch.object(threading.Thread,'start') as start:
            self.runtime.observe(SESSION)
            self.runtime.start(row)
        start.assert_not_called()

    def test_receiver_waits_for_native_turn_end_before_reading_or_injecting(self):
        row=self.row()
        with patch.object(self.runtime,'observe'):
            self.runtime.turn(SESSION,True)
        stop=threading.Event()
        def end_turn(_):
            with patch.object(self.runtime,'observe'):
                self.runtime.turn(SESSION,False)
        def receive(*args):
            stop.set()
            return []
        with patch.object(stop,'wait',side_effect=end_turn),patch.object(permissions,'current',return_value=True),patch.object(api,'tool',side_effect=receive) as tool:
            self.runtime.run(row,stop)
        tool.assert_called_once()
        self.assertEqual(self.ctx.injected,[])

    def test_withdraw_returns_exact_url_and_plugin_owned_restoration(self):
        row=self.row()
        state.put('permission','prior',{'value':None})
        with patch.object(api,'request',side_effect=[{'grant_id':GRANT},{'state':'disconnected'}]),patch.object(api,'tool',return_value={'epoch':1,'config':1}),patch.object(permissions,'restore'),patch.object(permissions,'current',return_value=None):
            result=self.tools.dispatch('pantheon_command',{'connection':CONNECTION,'op':'withdraw','key':'cleanup'},SESSION)
        self.assertEqual(result['state'],'disconnected')
        self.assertEqual(result['chat_url'],row['url'])
        self.assertIn('do not change it yourself',result['instruction'])
        self.assertEqual(result['incoming_replies'],{'current':'unset','captured_prior':'unset'})
        self.assertTrue(result['instruction'].startswith('Keep the chat link exact'))

    def test_readiness_and_terminal_carry_the_banner_and_exact_link(self):
        row = self.row()
        for status in ("waiting", "closed"):
            replies = [
                {"state": status, "epoch": 1, "config": 1},
                {"state": status},
            ]
            if status == "closed":
                replies.append({"chat": {"state": "closed"}, "result_agreed": False})
            with self.subTest(status=status), patch.object(api, "request", return_value={"grant_id": GRANT}), patch.object(api, "tool", side_effect=replies), patch.object(self.runtime, "start"):
                result = self.tools.dispatch("pantheon_ack_ready", {"connection": CONNECTION}, SESSION)
            self.assertEqual(result["chat_url"], row["url"])
            self.assertEqual(len(result["banner"].splitlines()), 3)
            if status == "waiting":
                self.assertIn("show them now", result["welcome_instruction"])
            else:
                self.assertIn("start this visible response with the same three-line banner", result["instruction"])
        self.assertEqual(len(result['banner'].splitlines()),3)

    def test_read_and_write_receipts_keep_exact_bound_url(self):
        row=self.row()
        for name,args in [
            ('pantheon_context',{}),
            ('pantheon_post',{'key':'post-marker','text':'Synthetic marker'}),
            ('pantheon_ack',{'key':'ack-marker','message_ids':['message']}),
        ]:
            with self.subTest(name=name),patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool',return_value={'id':'receipt','epoch':1,'config':1}) as remote,patch.object(self.runtime,'start'):
                result=self.tools.dispatch(name,{'connection':CONNECTION,**args},SESSION)
            self.assertEqual(result['chat_url'],row['url'])
            self.assertEqual(result['id'],'receipt')
            self.assertEqual(remote.call_count,1 if name=='pantheon_context' else 2)

    def test_incoming_setting_preserves_unset_false_and_true(self):
        for value,expected in [(None,'unset'),(False,'false'),(True,'true')]:
            with self.subTest(value=value),patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(permissions,'current',return_value=value):
                result=self.tools.dispatch('pantheon_doctor',{},SESSION)
            self.assertEqual(result['incoming_setting'],expected)
            self.assertEqual(result['incoming'],value is True)

    def test_durable_ack_flushes_file_and_directory(self):
        target=self.downloads/'receipt.txt';target.write_bytes(b'received')
        with patch.object(files.os,'fsync') as sync:
            self.assertEqual(files.read_file(str(target),root=self.downloads,durable=True,upload=False),b'received')
        self.assertEqual(sync.call_count,2)

    def test_downloads_cannot_write_outside_storage(self):
        data=b'peer data';meta={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        targets=[self.root/'autostart',self.ctx.cwd/'new.py',self.downloads/'..'/'outside',self.downloads/'.env']
        for target in targets:
            with self.subTest(path=target),self.assertRaises(api.APIError):
                files.save_file(str(target),base64.b64encode(data).decode(),meta)
            self.assertFalse(target.exists())
        outside=self.root/'outside';outside.mkdir()
        (self.downloads/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(api.APIError):files.save_file(str(self.downloads/'escape'/'new.py'),base64.b64encode(data).decode(),meta)
        self.assertFalse((outside/'new.py').exists())

    def test_uploads_are_confined_and_refuse_sensitive_paths(self):
        targets=[self.root/'outside.txt',self.ctx.cwd/'.env',self.ctx.cwd/'.env.production',
                 self.ctx.cwd/'.ssh'/'id_rsa',self.ctx.cwd/'.aws'/'config',self.ctx.cwd/'.config'/'gcloud'/'secret',
                 self.ctx.cwd/'.kube'/'config',self.ctx.cwd/'auth.json',self.ctx.cwd/'credentials.json']
        for target in targets:
            target.parent.mkdir(parents=True,exist_ok=True);target.write_text('synthetic secret')
            with self.subTest(path=target),self.assertRaises(api.APIError):files.read_file(str(target),root=self.ctx.cwd)
        valid=self.ctx.cwd/'public.txt';valid.write_bytes(b'public payload')
        self.assertEqual(files.read_file(str(valid),root=self.ctx.cwd),b'public payload')
        (self.ctx.cwd/'escape.txt').symlink_to(self.root/'outside.txt')
        with self.assertRaises(api.APIError):files.read_file(str(self.ctx.cwd/'escape.txt'),root=self.ctx.cwd)
        os.link(self.root/'outside.txt',self.ctx.cwd/'hardlink.txt')
        with self.assertRaises(api.APIError):files.read_file(str(self.ctx.cwd/'hardlink.txt'),root=self.ctx.cwd)
        os.mkfifo(self.ctx.cwd/'pipe')
        with self.assertRaises(api.APIError):files.read_file(str(self.ctx.cwd/'pipe'),root=self.ctx.cwd)

    def test_uploads_exclude_custom_hermes_home_even_beneath_workspace(self):
        profile=self.ctx.cwd/'custom-profile';profile.mkdir();target=profile/'nonsecret-name.txt';target.write_text('synthetic')
        with patch.object(files,'hermes_home',return_value=profile):
            with self.assertRaises(api.APIError):files.read_file(str(target),root=self.ctx.cwd)
            with self.assertRaises(api.APIError):files.bundle(str(profile),root=self.ctx.cwd)
            (self.ctx.cwd/'index.html').write_text('<p>Public</p>')
            with self.assertRaises(api.APIError):files.bundle(str(self.ctx.cwd),root=self.ctx.cwd)
            with patch.object(self.ctx,'session_cwd',return_value=str(profile)):
                with self.assertRaises(api.APIError):files.workspace_root(self.ctx)

    def test_invalid_workspace_cannot_expand_upload_authority(self):
        for value in ('', '.', '/', str(Path.home()), str(self.root/'profile')):
            Path(self.root/'profile').mkdir(exist_ok=True)
            with self.subTest(value=value),patch.object(self.ctx,'session_cwd',return_value=value),self.assertRaises(api.APIError):files.workspace_root(self.ctx)

    def test_file_rejections_precede_transfer_or_site_requests(self):
        self.row();outside=self.root/'outside.txt';outside.write_text('synthetic')
        cases=[('pantheon_transfer_send',{'path':str(outside)}),
               ('pantheon_site_deploy',{'directory':str(self.root),'key':'site'}),
               ('pantheon_transfer_fetch',{'path':str(outside),'transfer_id':'t'}),
               ('pantheon_transfer_ack',{'path':str(outside),'transfer_id':'t'})]
        with patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool') as remote:
            for name,args in cases:
                with self.subTest(tool=name),self.assertRaises(api.APIError):self.tools.dispatch(name,dict(args,connection=CONNECTION),SESSION)
            remote.assert_not_called()

    def test_ordinary_transfer_and_static_site_use_confined_files(self):
        self.row();source=self.ctx.cwd/'artifact.txt';source.write_bytes(b'artifact')
        self.assertEqual(files.read_file(str(source),root=self.ctx.cwd),b'artifact')
        site=self.ctx.cwd/'site';site.mkdir();(site/'index.html').write_text('<p>Hello</p>')
        bundle=files.bundle(str(site),root=self.ctx.cwd)
        self.assertEqual([item['path'] for item in bundle],['index.html'])
        self.assertEqual(base64.b64decode(bundle[0]['data']),b'<p>Hello</p>')
        with patch.object(api,'request',return_value={'grant_id':GRANT}),patch.object(api,'tool',return_value={}) as remote,patch.object(self.runtime,'start'):
            self.tools.dispatch('pantheon_transfer_send',{'connection':CONNECTION,'path':str(source),'key':'send','recipient':'peer','name':'artifact.txt','reason':'review'},SESSION)
        self.assertEqual(remote.call_args.args[3]['data'],base64.b64encode(b'artifact').decode())

    def test_site_bundles_reject_nested_secrets_and_symlinks(self):
        site=self.ctx.cwd/'site';site.mkdir();(site/'index.html').write_text('<p>Safe</p>')
        for name in ('.env','auth.json','.ssh','credentials.json'):
            target=site/name;target.write_text('synthetic secret')
            with self.subTest(name=name),self.assertRaises(api.APIError):files.bundle(str(site),root=self.ctx.cwd)
            target.unlink()
        (site/'leak').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(api.APIError):files.bundle(str(site),root=self.ctx.cwd)

    def test_parent_replacement_cannot_redirect_read_or_write(self):
        outside=self.root/'outside';outside.mkdir();(outside/'data.txt').write_text('secret')
        inside=self.ctx.cwd/'nested';inside.mkdir();(inside/'data.txt').write_text('public')
        original_open=os.open
        def racing_open(path,*args,**kwargs):
            if path=='nested':
                inside.rename(self.ctx.cwd/'original');inside.symlink_to(outside,target_is_directory=True)
            return original_open(path,*args,**kwargs)
        with patch.object(files.os,'open',side_effect=racing_open),self.assertRaises(OSError):
            files.read_file(str(inside/'data.txt'),root=self.ctx.cwd)
        destination=self.downloads/'nested';destination.mkdir()
        def racing_write(path,*args,**kwargs):
            if path=='nested':
                destination.rename(self.downloads/'original');destination.symlink_to(outside,target_is_directory=True)
            return original_open(path,*args,**kwargs)
        metadata={'size':4,'sha256':hashlib.sha256(b'data').hexdigest()}
        with patch.object(files.os,'open',side_effect=racing_write),self.assertRaises(OSError):
            files.save_file(str(destination/'new.txt'),base64.b64encode(b'data'),metadata)
        self.assertFalse((outside/'new.txt').exists())

    def test_missing_incoming_permission_requires_human_without_join_or_wait(self):
        args={'link':'https://pantheon.talk/join?invite='+'A'*43,'deliberate':True}
        with patch.object(api,'request',return_value={'grant_id':GRANT}) as request,patch.object(permissions,'current',return_value=False),patch.object(self.runtime,'start') as start:
            result=self.tools.dispatch('pantheon_connect',args,SESSION)
        self.assertEqual(result['error'],'incoming_permission_required')
        self.assertIn('Show the human',result['instruction']);self.assertIn('do not execute',result['instruction'])
        self.assertIn('End the turn',result['instruction']);self.assertIn('without waiting',result['instruction'])
        self.assertEqual([c.args[:2] for c in request.call_args_list],[('GET','/identity')]);start.assert_not_called()

    def test_recovered_join_uses_current_disconnected_state(self):
        from hermes.pantheon_native.tools import recover
        old=self.row();current=dict(old,state='disconnected')
        intent=state.intent('join',SESSION,GRANT,'old')
        with patch.object(api,'request',side_effect=[old,current]):
            row=recover(intent,SESSION,GRANT)
        with patch.object(threading.Thread,'start') as start:
            self.runtime.start(row)
        self.assertEqual(row['state'],'disconnected');start.assert_not_called()

class HostStorageTests(unittest.TestCase):
    def setUp(self):
        try:
            import hermes_constants
            import plugins.plugin_storage
        except ImportError:
            self.skipTest('real Hermes host supplied by the isolated contract runner')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.profile=Path(self.temp.name).resolve()/'profile';self.profile.mkdir()
        selected=patch.object(hermes_constants,'get_hermes_home',return_value=self.profile)
        selected.start();self.addCleanup(selected.stop)

    def test_download_directory_uses_public_profile_storage(self):
        root=files.download_root()
        self.assertEqual(root,self.profile/'plugin-data/pantheon/downloads')
        self.assertEqual(root.stat().st_mode & 0o777,0o700)
        data=b'new-user download';metadata={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        result=files.save_file(str(root/'artifact.txt'),base64.b64encode(data),metadata)
        self.assertTrue(result['durable'])
        self.assertEqual(files.read_file(result['path'],root=root,upload=False),data)

    def test_download_directory_rejects_replaced_storage(self):
        root=files.download_root();root.rmdir()
        outside=Path(self.temp.name).resolve()/'outside';outside.mkdir()
        root.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):files.download_root()
        self.assertEqual(list(outside.iterdir()),[])

if __name__=='__main__':unittest.main()
