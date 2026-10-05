"""Transport identity/cache tests; emitted programs use the selected real compiler."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('producer_under_test',ROOT/'tools/vm_provider.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class InspectionTests(unittest.TestCase):
    def test_compiled_input_and_repin_invalidates_warm_inspection(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'source').write_text('one');exe=root/'probe';exe.write_text('executable')
            (root/'mncs-language.lock.json').write_text(json.dumps({'revision':'pinned'}))
            receipt={'source_inputs':{'source':module.file_digest(root/'source'),'mncs-language.lock.json':module.file_digest(root/'mncs-language.lock.json')},'stage0_revision':'pinned'}
            producer={'receipt':receipt,'identity':'sha256:'+module.digest(receipt)}
            with patch.object(module.subprocess,'run',return_value=type('R',(),{'stdout':json.dumps(producer)})()) as run:
                p=module.CompilerProvider(root,executable=exe)
                self.assertEqual(p.inspect()['state'],'ready');p.inspect();self.assertEqual(run.call_count,1)
                (root/'source').write_text('two')
                self.assertEqual(p.inspect()['state'],'stale')
                (root/'source').write_text('one');(root/'mncs-language.lock.json').write_text(json.dumps({'revision':'repinned'}))
                self.assertIn('stage0-pin',p.inspect()['mismatches'])

@unittest.skipUnless((ROOT/'.bootstrap/target/release/mncs-compiler-stage0-probe').is_file(),'build selected producer first')
class DirectProductTests(unittest.TestCase):
    def test_cache_reuse_tamper_repair_and_source_invalidation(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/'sample.mncs';source.write_text('mncs 0.18; module proof.integration.v1; fn value() -> (result: i64) { return 7; }')
            p=module.CompilerProvider(ROOT);request={'schema_version':'mncs.compiler-vm-request/1','source':str(source),'logical_name':'sample.mncs','libraries':[]}
            first=p.emit(request,root/'cache');self.assertFalse(first['cache_reused'])
            second=p.emit(request,root/'cache');self.assertTrue(second['cache_reused']);self.assertEqual(first['artifact'],second['artifact'])
            path=Path(first['product']);corrupt=json.loads(path.read_text());corrupt['artifact']['identity']='forged';path.write_text(json.dumps(corrupt))
            repaired=p.emit(request,root/'cache');self.assertFalse(repaired['cache_reused']);self.assertEqual(repaired['artifact'],first['artifact'])
            artifact_path=root/'cache'/first['artifact']['address'];stamp=artifact_path.stat().st_ino
            other=p.emit({**request,'calls':[{'schema_version':'0.1','target':{'module':'proof.integration.v1','function':'value'},'arguments':[],'step_budget':100}]},root/'cache')
            self.assertEqual(other['artifact'],first['artifact']);self.assertEqual(artifact_path.stat().st_ino,stamp)
            source.write_text(source.read_text().replace('return 7','return 9'))
            # Source-directory resolution is an input too, even without explicit libraries.
            (root/'helper.mncs').write_text('mncs 0.18; module proof.helper.v1; fn helper() -> (result: i64) { return 2; }')
            changed=p.emit(request,root/'cache');self.assertNotEqual(changed['artifact']['identity'],first['artifact']['identity'])
            self.assertNotEqual(changed['build_receipt']['identity'],first['build_receipt']['identity'])

    def test_adjacent_import_change_invalidates_without_source_change(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/'main.mncs';helper=root/'helper.mncs'
            source.write_text('mncs 0.18;\nmodule proof.integration.v1;\nuse proof.helper as lib;\nfn value() -> (result: i64) { return lib.value(); }')
            helper.write_text('mncs 0.18;\nmodule proof.helper;\nfn value() -> (result: i64) { return 2; }')
            p=module.CompilerProvider(ROOT);request={'schema_version':'mncs.compiler-vm-request/1','source':str(source),'logical_name':'main.mncs'}
            first=p.emit(request,root/'cache');self.assertTrue(p.emit(request,root/'cache')['cache_reused'])
            helper.write_text(helper.read_text().replace('return 2','return 3'))
            second=p.emit(request,root/'cache');self.assertFalse(second['cache_reused'])
            self.assertNotEqual(first['artifact']['identity'],second['artifact']['identity'])

if __name__=='__main__':unittest.main()
