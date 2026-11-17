// Called by Python inside actions/github-script, inheriting GitHub's runtime credentials.
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const root = path.resolve(process.argv[2]);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
  const {DefaultArtifactClient} = require(process.env.FACTORY_ARTIFACT_MODULE);
  const client = new DefaultArtifactClient();
  const files = ['manifest.json', ...Object.keys(manifest.files)].map(f => path.join(root, f));
  const result = await client.uploadArtifact(
    `factory-checkpoint-${manifest.run_id}-${manifest.generation}`, files, root, {retentionDays: 14}
  );
  if (!result.id) throw new Error('Missing artifact ID');
  console.log(result.id);
})().catch(e => { console.error(e.message); process.exitCode = 1; });
