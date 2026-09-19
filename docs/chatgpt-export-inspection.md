# Private Export structure inspection v0

This is the first, **read-only diagnostic** part of #62 / #66, not an importer.
It establishes what the user's local Export looks like without publishing its
conversation text. It neither normalizes conversations nor queues Knowledge jobs.

## Run locally

With the package installed, use Python 3.11+:

```bash
python -B -m obsidian_automation.chatgpt_export_inspect /path/to/export.zip
```

The module is standard-library-only and also runs as a standalone file:

```bash
python3 -B chatgpt_export_inspect.py /path/to/export.zip > export-structure.json
```

Run as the unprivileged account that owns the completed download. Do not run as
root or from an AI identity with Vault or credential access. No network connection,
ZIP extraction, output file creation, package installation, or service change is
performed by the tool. Shell redirection in the example creates the report file;
use an appropriate private directory. `-B` also disables Python bytecode writes.
Local access may update filesystem access timestamps; this is not byte immutability.

Only the resulting report is needed for schema investigation. Do not upload the
original Export, a chat sample, account data, or an archive listing. Review even
the report before sharing: structural counts themselves can be private.

## Selection is explicit and visible

Without `--member`, this implementation recognizes root-level `conversations.json`
or candidate numbered names matching `conversations-<digits>.json` and
`conversations_<digits>.json`. These are conservative diagnostic candidates, NOT
an assertion about every current or future Export filename. Mixed singleton and
numbered candidates require an explicit selection to avoid double counting.
For another layout, select exact local JSON member names without disclosing them:

```bash
python3 -B chatgpt_export_inspect.py /path/to/export.zip \
  --member 'folder/selected-conversation-file.json'
```

Repeat `--member` for multiple members. A separately prepared local JSON is also
supported with `--format json`. The program does not prepare or split that file.
Unselected JSON members are counted, not read or CRC-verified. It never claims
that all account conversations were covered; the selection counts are essential.

OpenAI's official documentation describes a ZIP and `conversations.json`, with
numbered conversation JSON files possible in larger exports. It does not define
the full internal message schema or a numbered-filename contract here:

- https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data
- https://help.openai.com/en/articles/9106926-transfer-exported-conversations-between-chatgpt-accounts

These references describe acquisition only; they are not proof that the synthetic
mapping/current_node fixtures match the user's real Export.

## Output contract

The result has `record_version=1` and `inspection_only=true`. It contains only
fixed field names, fixed enum/type buckets, booleans, and aggregate counts:

- selected/unselected JSON and archive entry counts;
- conversation, graph-node, message, and branching-node counts;
- types/missing counts of allowlisted conversation/node/message/author/content fields;
- unknown-key occurrence counts, never the unknown keys;
- allowlisted role/content-type and part-type counts; unfamiliar values are `other`;
- resolution status of the current_node -> parent path, without IDs or contents.

No title, text, source IDs, timestamp values, filenames, input paths, arbitrary
keys, or arbitrary enum values are copied to the report. CLI errors are fixed
codes rather than raw exceptions. No excerpts of malformed JSON are printed.
Counts of messages/roles cover the inspected mapping, including alternate branches.
They are not counts of final-branch messages to be imported. Parent-chain lengths
include the traversed prefix for unresolved chains. The tool does not validate
all graph branches, reciprocal children relationships, visibility, or semantics.

Exit codes:

- `0`: inspection finished (`inspected`), NOT import success;
- `3`: structural review required, including empty/unknown roots, missing mapping,
  an unresolved current path, or unknown role/content shapes;
- `2`: unsafe/unsupported input, limits, invalid JSON, unreadable input, or bad arguments.

Always retain `full_graph_validated=false` and
`import_compatibility_established=false`, even with exit code 0.
This schema profile is evidence for subsequent parser design, not final acceptance.

## Limits and failure boundaries

Defaults: 4 GiB archive, 8 MiB central directory, 20,000 archive entries,
128 MiB per selected JSON, 256 MiB total selected JSON, 200:1 expansion ratio,
50,000 conversations and 500,000 nodes. A central-directory preflight happens
before ZipFile allocation. ZIP64 central directories and split/multidisk archives
are not supported in v0; selected entries must be stored or deflated.

All member names are checked for unsafe components, control characters,
case-folded duplicates, special file types and encryption. No member is extracted.
Selected entries have bounded reads and ZIP CRC checking. Duplicate JSON keys,
invalid encodings, non-finite numbers and excessive parser nesting are rejected.
Input descriptor metadata is checked before/after inspection to detect common
concurrent modification. This is not a hostile-writer snapshot proof: inspect a
finished file under a trusted local parent directory. The final path is opened
without following a symlink on Linux; arbitrary ancestor symlinks are not sandboxed.

Selected JSON is parsed in memory one member at a time. Encoded-byte limits are
NOT a hard RAM/CPU sandbox; Python objects can occupy substantially more memory.
Use a suitable local machine and do not remove limits to process a large Export.
A streaming parser and stronger worker resource limits are separate future work.

## Next boundary

Real structural evidence precedes the bounded normalizer, private durable intake,
source revision/delta comparison, external Source/Context contract, and integration
with #64. Unknown fields/branches/attachments must not silently become normalized
facts. No Generator, Validator, Review, Executor, Transport, or credential authority
is added here. Automatic acquisition, timers and canonical writes remain absent.
