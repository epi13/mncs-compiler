"""Transport identity/cache tests; emitted programs use the selected real compiler."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('producer_under_test',ROOT/'tools/vm_provider.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class InspectionTests(unittest.TestCase):
    @staticmethod
    def toolchain(root):
        values={}
        for name in ('cargo','rustc'):
            path=root/name;path.write_text(name+'-bytes')
            values[name]={'configured_path':str(path),'resolved_path':str(path.resolve()),'sha256':module.file_digest(path)}
        return values

    def test_build_tool_bytes_are_part_of_producer_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);toolchain=self.toolchain(root)
            self.assertEqual(module.toolchain_mismatches({'toolchain_executables':toolchain}),[])
            (root/'rustc').write_text('changed')
            self.assertEqual(module.toolchain_mismatches({'toolchain_executables':toolchain}),['build-tool:rustc'])
            self.assertEqual(module.toolchain_mismatches({}),['toolchain-executable-identities'])

    def test_compiled_input_and_repin_invalidates_warm_inspection(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/'source').write_text('one');exe=root/'probe';exe.write_text('executable')
            (root/'mncs-language.lock.json').write_text(json.dumps({'revision':'pinned'}))
            self.toolchain(root)
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            subprocess.run(['git','config','user.email','test@example.invalid'],cwd=root,check=True)
            subprocess.run(['git','config','user.name','Provider Test'],cwd=root,check=True)
            subprocess.run(['git','add','.'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','fixture'],cwd=root,check=True)
            revision=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
            receipt={'source_inputs':{'source':module.file_digest(root/'source'),'mncs-language.lock.json':module.file_digest(root/'mncs-language.lock.json')},'stage0_revision':'pinned','build_configuration':{'toolchain_executables':self.toolchain(root)}}
            receipt.update(source_revision=revision,dirty_content_identity=module.digest({}),dirty_input_count=0)
            producer={'receipt':receipt,'identity':'sha256:'+module.digest(receipt)}
            real_run=module.subprocess.run;producer_calls=[]
            def selected_run(args,**kwargs):
                if args[0]=='git':return real_run(args,**kwargs)
                producer_calls.append(args)
                return type('R',(),{'stdout':json.dumps(producer)})()
            with patch.object(module.subprocess,'run',side_effect=selected_run):
                p=module.CompilerProvider(root,executable=exe)
                self.assertEqual(p.inspect()['state'],'ready');p.inspect();self.assertEqual(len(producer_calls),1)
                (root/'notes.txt').write_text('unrelated authored context')
                self.assertEqual(p.inspect()['state'],'ready')
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
