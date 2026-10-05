#!/usr/bin/env python3
"""Provider-owned direct artifact transport and local build-origin observations.

The pinned compiler owns source resolution, verification, SSA and correspondence;
this module only transports files, receipts and exact executable identities.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SCHEMA = 'mncs.compiler-vm-product/1'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def toolchain_mismatches(configuration):
    tools = configuration.get('toolchain_executables') if isinstance(configuration, dict) else None
    if not isinstance(tools, dict) or set(tools) != {'cargo', 'rustc'}:
        return ['toolchain-executable-identities']
    mismatches = []
    for name, identity in tools.items():
        try:
            if not isinstance(identity, dict):
                raise ValueError('invalid tool identity')
            configured_path = identity['configured_path']
            configured = Path(configured_path)
            if not configured.is_absolute() and configured.parent == Path('.'):
                configured = Path(shutil.which(configured_path) or configured_path)
            configured = configured.resolve(strict=True)
            resolved = Path(identity['resolved_path']).resolve(strict=True)
            if configured != resolved or file_digest(resolved) != identity['sha256']:
                mismatches.append('build-tool:' + name)
        except (OSError, KeyError, TypeError, ValueError):
            mismatches.append('build-tool:' + name)
    return mismatches


class ProviderError(RuntimeError):
    pass


class CompilerProvider:
    def __init__(self, checkout: Path, *, executable: Path | None = None, timeout: float = 120):
        self.checkout = Path(checkout).resolve()
        self.executable = Path(executable or self.checkout / '.bootstrap/target/release/mncs-compiler-stage0-probe').resolve()
        self.timeout = timeout
        self._inspection = None
        self._inspection_signature = None
        self._inputs_cache = None
        self._inputs_signature = None

    @staticmethod
    def _stamp(path):
        try:
            stat = Path(path).stat()
            return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        except OSError:
            return None

    def inspect(self):
        if self._inspection is not None:
            signature = tuple((name, self._stamp(self.checkout / name)) for name in self._inspection['producer']['receipt']['source_inputs']) + ((str(self.executable), self._stamp(self.executable)),)
            if signature == self._inspection_signature:
                return self._inspection
        if not self.executable.is_file():
            return {'schema_version': 'mncs.compiler-vm-provider/1', 'state': 'unavailable', 'executable': str(self.executable)}
        completed = subprocess.run([str(self.executable), '--producer-info'], capture_output=True, text=True, timeout=self.timeout, check=True)
        producer = json.loads(completed.stdout)
        receipt = producer['receipt']
        if producer['identity'] != 'sha256:' + digest(receipt):
            raise ProviderError('producer build receipt identity mismatch')
        mismatches = []
        mismatches.extend(toolchain_mismatches(receipt.get('build_configuration')))
        try:
            revision = subprocess.run(['git', '-C', str(self.checkout), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            status = subprocess.run(['git', '-C', str(self.checkout), 'status', '--porcelain=v1', '--untracked-files=all'], capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
            changed = {line[3:].rsplit(' -> ', 1)[-1] for line in status if len(line) >= 4}
            dirty_inputs = {name: identity for name, identity in receipt['source_inputs'].items() if name in changed}
            if revision != receipt.get('source_revision'):
                mismatches.append('source-revision')
            if digest(dirty_inputs) != receipt.get('dirty_content_identity') or len(dirty_inputs) != receipt.get('dirty_input_count'):
                mismatches.append('dirty-checkout-identity')
        except (OSError, subprocess.SubprocessError):
            mismatches.append('checkout-state-unavailable')
        for name, expected in receipt['source_inputs'].items():
            path = (self.checkout / name).resolve()
            if not path.is_relative_to(self.checkout) or not path.is_file() or file_digest(path) != expected:
                mismatches.append(name)
        pin = json.loads((self.checkout / 'mncs-language.lock.json').read_text())
        if pin['revision'] != receipt['stage0_revision']:
            mismatches.append('stage0-pin')
        value = {'schema_version': 'mncs.compiler-vm-provider/1', 'state': 'stale' if mismatches else 'ready',
                'checkout': str(self.checkout), 'executable': str(self.executable),
                'executable_sha256': file_digest(self.executable), 'producer': producer,
                'build_origin': 'input-mismatch' if mismatches else 'locally-observed-compiled-inputs',
                'build_origin_mismatches': mismatches,
                'mismatches': mismatches, 'artifact_schema': 'mncs.vm.artifact/1', 'ssa_schema': '0.5',
                'vm_contract': 'mncs.vm/0.1', 'source_map_schema': 'mncs.execution-source-map/1',
                'reference_role': 'pinned Stage-0 bootstrap; no self-hosting claim'}
        self._inspection = value
        self._inspection_signature = tuple((name, self._stamp(self.checkout / name)) for name in receipt['source_inputs']) + ((str(self.executable), self._stamp(self.executable)),)
        return value

    def _inputs(self, request, info):
        source = Path(request['source']).resolve()
        libraries = [Path(p).resolve() for p in request.get('libraries', [])]
        roots = [source.parent, *libraries]
        inventories = []
        for root in roots:
            if not root.is_dir():
                raise ProviderError('selected source/library root unavailable')
            paths = []
            for path in root.rglob('*.mncs'):
                paths.append(path)
                if len(paths) > 4096:
                    raise ProviderError('selected source/library root exceeds bounded file inventory')
            inventories.append(sorted(paths))
        signature = (digest(request), info['producer']['identity'], info['executable_sha256'], self._stamp(source),
            self._stamp(__file__), self._stamp(self.checkout / '.mncs/project.json'),
            tuple(tuple((str(p), self._stamp(p)) for p in paths) for paths in inventories))
        if self._inputs_cache is not None and signature == self._inputs_signature:
            return self._inputs_cache
        material = {'source': file_digest(source), 'libraries': []}
        for root, paths in zip(roots, inventories):
            if not root.is_dir():
                raise ProviderError('selected library root unavailable')
            if len(paths) > 4096:
                raise ProviderError('selected library exceeds bounded file inventory')
            rows = []
            for path in paths:
                if not path.resolve().is_relative_to(root):
                    raise ProviderError('library symlink escapes selected root')
                rows.append([path.relative_to(root).as_posix(), file_digest(path)])
            material['libraries'].append(rows)
        declaration = json.loads((self.checkout / '.mncs/project.json').read_text())
        value = {'provider':'mncs-compiler:canonical-vm-artifact', 'declaration_identity':digest(declaration),
                'dependency_roots':[{'ordinal':i, 'content_identity':digest(rows)} for i,rows in enumerate(material['libraries'])],
                'stdlib':{'selection':'explicit library roots; compiler records actual resolved closure'},
                'abi':{'artifact_schema':'mncs.vm.artifact/1','ssa_schema':'0.5','vm_contract':'mncs.vm/0.1'},
                'producer': info['producer']['identity'], 'compiler_artifact_sha256': info['executable_sha256'],
                'source_inputs': material, 'configuration': {**{k: v for k, v in request.items() if k not in ('source', 'libraries')},
                    'selected_inputs':{'source':str(source),'roots':[str(root) for root in roots],
                        'compiler_checkout':str(self.checkout),'compiler_executable':str(self.executable)}},
                'build_transport_sha256': file_digest(__file__)}
        self._inputs_signature, self._inputs_cache = signature, value
        return value

    def emit(self, request, cache: Path):
        info = self.inspect()
        if info['state'] != 'ready':
            raise ProviderError('selected producer unavailable or stale: ' + str(info.get('mismatches', [])))
        if request.get('schema_version') != 'mncs.compiler-vm-request/1':
            raise ProviderError('unsupported compiler request schema')
        inputs = self._inputs(request, info)
        cache = Path(cache).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        key = digest(inputs)
        product_path = cache / f'{key}.json'
        # Serialize preparation across consumers. Artifacts remain ordinary immutable files.
        import fcntl
        with (cache / '.prepare.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if product_path.is_file():
                try:
                    product = json.loads(product_path.read_text())
                    receipt = product['build_receipt']
                    artifact = cache / product['artifact']['address']
                    evidence = cache / product['evidence']['address']
                    if (product['schema_version'] == PRODUCT_SCHEMA and receipt['core']['inputs'] == inputs
                            and receipt['identity'] == digest(receipt['core'])
                            and product['artifact'] == receipt['core']['artifact']
                            and product['evidence'] == receipt['core']['evidence']
                            and artifact.resolve().is_relative_to(cache) and evidence.resolve().is_relative_to(cache)
                            and file_digest(artifact) == product['artifact']['sha256']
                            and file_digest(evidence) == product['evidence']['sha256']):
                        return {**product, 'producer':info, 'cache_reused': True, 'cache': str(cache), 'product': str(product_path)}
                except (ValueError, KeyError, OSError):
                    pass
            with tempfile.TemporaryDirectory(prefix='request-', dir=cache) as directory:
                path = Path(directory) / 'request.json'
                path.write_bytes(encoded(request))
                result = subprocess.run([str(self.executable), f'--provider-request={path}'], cwd=self.checkout,
                                        capture_output=True, timeout=self.timeout, check=False)
            if result.returncode:
                raise ProviderError(result.stderr.decode(errors='replace')[-3000:])
            emitted = json.loads(result.stdout)
            if emitted['status'] != 'emitted':
                raise ProviderError('compiler refused source: ' + json.dumps(emitted)[:3000])
            if emitted['producer']['identity'] != info['producer']['identity'] or self._inputs(request, self.inspect()) != inputs:
                raise ProviderError('producer or source inputs moved during compilation')
            artifact = emitted.pop('artifact')
            if not ('compiler-producer:' + info['producer']['identity']) in artifact['source']['lowering_refs']:
                raise ProviderError('artifact omitted producer binding')
            artifact_bytes = encoded(artifact)
            artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
            artifact_ref = {'address': artifact_sha + '.artifact.json', 'sha256': artifact_sha,
                            'identity': artifact['artifact_id'], 'schema': artifact['schema_version'], 'schema_version':artifact['schema_version'],
                            'payload_sha256':artifact_sha, 'interface_identity':artifact['source'].get('interface_identity'),
                            'backend':{'name':artifact['source']['backend_name'],'version':artifact['source']['backend_version']},
                            'target':artifact['source']['target']['identity'], 'bytes': len(artifact_bytes)}
            evidence_bytes = encoded(emitted)
            evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
            evidence_ref = {'address': evidence_sha + '.evidence.json', 'sha256': evidence_sha}
            core = {'provider': 'mncs-compiler:canonical-vm-artifact', 'inputs': inputs, 'artifact': artifact_ref,
                    'producer': info['producer']['identity'], 'evidence': evidence_ref,
                    'build_capability':'mncs-compiler:canonical-vm-artifact', 'build_invocation_identity':digest(inputs),
                    'remediation_admission':{'status':'not_requested','basis':'explicit producer invocation; no remediation claim'},
                    'inventory_identity': digest(emitted['callable_bindings']), 'source_map_identity': (emitted.get('source_map') or {}).get('identity')}
            product = {'schema_version': PRODUCT_SCHEMA, 'artifact': artifact_ref, 'evidence': evidence_ref,
                       'build_receipt': {'schema_version': 'mncs.provider-build-receipt/1', 'identity': digest(core), 'core': core},
                       'producer': info, 'module': emitted['module']}
            for name, data in [(artifact_ref['address'], artifact_bytes), (evidence_ref['address'], evidence_bytes), (product_path.name, encoded(product))]:
                destination = cache / name
                # Equal immutable products keep their inode/stamp for retained
                # readers, even when a different request shares these bytes.
                if name != product_path.name and destination.is_file() and file_digest(destination) == hashlib.sha256(data).hexdigest():
                    continue
                temporary = cache / ('.' + name + f'.{os.getpid()}.tmp')
                temporary.write_bytes(data)
                os.replace(temporary, destination)
            return {**product, 'cache_reused': False, 'cache': str(cache), 'product': str(product_path)}


def build_selected_producer(*, checkout: Path, executable: Path, cargo: str = 'cargo'):
    """Rebuild the pinned direct-emitter probe from its selected Stage-0 tree."""
    checkout = Path(checkout).resolve()
    expected = (checkout / '.bootstrap/target/release/mncs-compiler-stage0-probe').resolve()
    if Path(executable).resolve() != expected:
        raise ProviderError('provider build is bound to the selected Stage-0 probe path')
    pin = json.loads((checkout / 'mncs-language.lock.json').read_text())['revision']
    staged = checkout / '.bootstrap'
    if (not (staged / 'Cargo.toml').is_file()
            or (staged / 'revision').read_text().strip() != pin):
        raise ProviderError('selected Stage-0 source tree is absent or does not match the compiler pin')
    result = subprocess.run(
        ['bash', str(checkout / 'tools/bootstrap.sh')], cwd=checkout,
        capture_output=True, text=True, timeout=1800, check=False,
        env={**os.environ, 'CARGO': cargo, 'GIT_OPTIONAL_LOCKS': '0'},
    )
    if result.returncode:
        raise ProviderError('selected compiler producer build failed: ' + result.stderr[-3000:])
    provider = CompilerProvider(checkout, executable=expected)
    inspection = provider.inspect()
    if inspection.get('state') != 'ready':
        raise ProviderError('rebuilt compiler producer does not match its recorded source inputs')
    return {'schema_version': 'mncs.compiler-build-operation/1',
            'status': 'built-and-verified-locally', 'executable': str(expected),
            'producer_identity': inspection['producer']['identity'],
            'executable_sha256': inspection['executable_sha256'],
            'source_revision': inspection['producer']['receipt'].get('source_revision'),
            'stage0_revision': inspection['producer']['receipt'].get('stage0_revision'),
            'build_command': ['bash', 'tools/bootstrap.sh'],
            'build_stderr_tail': result.stderr[-1000:]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['inspect', 'emit', 'build'])
    parser.add_argument('--request')
    parser.add_argument('--cache')
    parser.add_argument('--executable', default=os.environ.get('MNCS_COMPILER_PROBE'))
    parser.add_argument('--cargo', default=os.environ.get('CARGO', 'cargo'))
    args = parser.parse_args(argv)
    provider = CompilerProvider(ROOT, executable=Path(args.executable) if args.executable else None)
    try:
        if args.operation == 'inspect':
            value = provider.inspect()
        elif args.operation == 'build':
            value = build_selected_producer(checkout=ROOT, executable=provider.executable,
                                            cargo=args.cargo)
        else:
            if not args.request or not args.cache:
                parser.error('emit requires --request and --cache')
            value = provider.emit(json.loads(Path(args.request).read_text()), Path(args.cache))
        print(json.dumps(value, sort_keys=True))
        return 0
    except (ProviderError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'schema_version': 'mncs.compiler-vm-provider/1', 'state': 'error', 'reason': str(error)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
