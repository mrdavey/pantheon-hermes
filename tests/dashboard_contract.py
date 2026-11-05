"""Run with the real Hermes interpreter (FastAPI/Pydantic are host dependencies)."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

spec=importlib.util.spec_from_file_location('pantheon_dashboard_contract',Path(__file__).parents[1]/'dashboard/plugin_api.py')
api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
GRANT='grant_01234567890123456';CONNECTION='connection_01234567890';OP='operation_012345678901'

class DashboardContract(unittest.TestCase):
 def setUp(self):
  app=FastAPI();app.include_router(api.router);self.client=TestClient(app)
 def test_management_inventory_has_no_agent_proxy(self):
  paths=set(self.client.get('/openapi.json').json()['paths'])
  self.assertEqual(paths,{'/connections','/connections/{connection}/disconnect','/operations/{operation}','/authorization/revoke'})
 def test_list_fences_reauthentication(self):
  with patch.object(api,'call',side_effect=[{'grant_id':GRANT},{'connections':[]},{'grant_id':'other_grant_0123456789'}]):
   response=self.client.get('/connections')
  self.assertEqual(response.status_code,409);self.assertNotIn('connections',response.json())
 def test_disconnect_rejects_spoofed_grant_and_extra_authority(self):
  with patch.object(api,'call',return_value={'grant_id':GRANT}) as call:
   response=self.client.post('/connections/'+CONNECTION+'/disconnect',json={'grant':'other_grant_0123456789','operation':OP})
   self.assertEqual(response.status_code,409);self.assertEqual(call.call_count,1)
   response=self.client.post('/connections/'+CONNECTION+'/disconnect',json={'grant':GRANT,'operation':OP,'session':'forged'})
   self.assertEqual(response.status_code,422);self.assertEqual(call.call_count,1)
 def test_disconnect_and_receipt_preserve_key(self):
  with patch.object(api,'call',side_effect=[{'grant_id':GRANT},{'id':CONNECTION,'state':'disconnected'}]) as call:
   response=self.client.post('/connections/'+CONNECTION+'/disconnect',json={'grant':GRANT,'operation':OP})
  self.assertEqual(response.status_code,200)
  self.assertEqual(call.call_args.args,('POST','/connections/'+CONNECTION+'/disconnect',{'operation':OP}))
 def test_native_helper_errors_are_redacted(self):
  with patch('hermes_cli.plugin_provider_requests.credentialed_provider_request',side_effect=RuntimeError('secret_token=PRIVATE')):
   response=self.client.get('/connections')
  self.assertEqual(response.status_code,503);self.assertNotIn('PRIVATE',response.text)

if __name__=='__main__':unittest.main()
