import contextlib
import contextvars
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
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
        self.tools=Tools(self.ctx,self.runtime)
    def row(self, **extra):
        value={'id':CONNECTION,'chat':'room','member':'hermes','url':'https://pantheon.talk/app?chat=room',
               'deadline':'2099-01-01T00:00:00Z','state':'connected','session':dict(SESSION),'grant':GRANT}
        value.update(extra);state.put('connection',CONNECTION,value);return value
    def test_registration_inert_and_exact(self):
        with patch.object(state,'database',side_effect=AssertionError('storage at registration')),patch.object(api,'request',side_effect=AssertionError('network at registration')),patch.object(threading.Thread,'start',side_effect=AssertionError('thread at registration')):
            register(self.ctx)
        manifest=json.loads((Path(__file__).parents[1]/'plugin.yaml').read_text())
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
    def test_download_integrity_no_overwrite_and_explicit_receipt(self):
        import base64,hashlib
        target=Path(self.temp.name)/'saved.txt';data=b'complete payload'
        meta={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        receipt=files.save_file(str(target),base64.b64encode(data).decode(),meta)
        self.assertTrue(receipt['durable']);self.assertEqual(target.read_bytes(),data)
        target.write_text('changed')
        with self.assertRaises(api.APIError):files.save_file(str(target),base64.b64encode(data).decode(),meta)
        with self.assertRaises(api.APIError):files.save_file(str(target),'eA==',meta)
    def test_error_redaction_and_no_authority_arguments(self):
        with patch('hermes.pantheon_native.tools.observe',return_value=SESSION),patch.object(api,'request',side_effect=RuntimeError('secret access_token=PRIVATE')):
            result=self.tools.call('pantheon_doctor',{})
            self.assertNotIn('PRIVATE',result)
            self.assertIn('invalid_arguments',self.tools.call('pantheon_doctor',{'session':'forged'}))

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
        with patch.object(api,'request',side_effect=[{'grant_id':GRANT},{'state':'disconnected'}]),patch.object(api,'tool',return_value={'epoch':1,'config':1}),patch.object(permissions,'restore'):
            result=self.tools.dispatch('pantheon_command',{'connection':CONNECTION,'op':'withdraw','key':'cleanup'},SESSION)
        self.assertEqual(result['state'],'disconnected')
        self.assertEqual(result['chat_url'],row['url'])
        self.assertIn('do not change it yourself',result['instruction'])

    def test_durable_ack_flushes_file_and_directory(self):
        target=Path(self.temp.name)/'receipt.txt';target.write_bytes(b'received')
        with patch.object(files.os,'fsync') as sync:
            self.assertEqual(files.read_file(str(target),durable=True),b'received')
        self.assertEqual(sync.call_count,2)

    def test_recovered_join_uses_current_disconnected_state(self):
        from hermes.pantheon_native.tools import recover
        old=self.row();current=dict(old,state='disconnected')
        intent=state.intent('join',SESSION,GRANT,'old')
        with patch.object(api,'request',side_effect=[old,current]):
            row=recover(intent,SESSION,GRANT)
        with patch.object(threading.Thread,'start') as start:
            self.runtime.start(row)
        self.assertEqual(row['state'],'disconnected');start.assert_not_called()

if __name__=='__main__':unittest.main()
