"""Exercise clipboard denial, stale/dirty guards and ordered post copying."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_clipboard_handoff_uses_exact_saved_package_and_falls_back_safely():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise the publication controller")
    module = Path(__file__).resolve().parents[2] / "src/fieldnote/web/publishing.js"
    script = "import { createPublishingController } from " + json.dumps(module.as_uri()) + ";\n" + r"""
import assert from 'node:assert/strict';
class Element {
  constructor(id = '') { this.id = id; this.value = ''; this.attributes = new Map(); this.listeners = new Map(); this.children = []; }
  addEventListener(name, handler) { this.listeners.set(name, handler); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  async fire(name = 'click') { return this.listeners.get(name)?.(); }
}
const elements = new Map();
const created = [];
const copies = ['copy-publication-title','copy-publication-article','copy-publication-formatted','copy-linkedin','copy-x'];
const $ = id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); };
globalThis.document = {
  getElementById: $, createElement: () => { const e = new Element(); created.push(e); return e; },
  querySelectorAll: () => [...copies.map($), ...created.filter(e => e.attributes.has('data-package-copy'))],
};
globalThis.Option = class {constructor(text, value) { this.text = text; this.value = value; }};
const writes = [], notices = [];
const clipboard = {writeText: async text => {writes.push({text});}, write: async items => {writes.push({items});}};
Object.defineProperty(globalThis, 'navigator', {value: {clipboard}, configurable: true});
globalThis.ClipboardItem = class {constructor(types) {this.types = types;}};
const state = {activeRun: 'run-a', article: {id: 'revision-a',content_hash: 'hash-a'},
  revisions: [{id: 'revision-a'}], editorDirty: false,publicationDirty: false,saving: false};
let value = {id: 'package-a',stale: false,data: {
  revision_id: 'revision-a',content_hash:'hash-a',title:'Saved title',medium_markdown:'# Saved title\n\nBody',
  medium_body_text:'Disclosure\n\nExact body\n',medium_body_html:'<p>Disclosure</p><p><strong>Exact body</strong></p>',
  linkedin_text:'LinkedIn saved text',x_posts:['First post','Second post'],canonical_url:null,disclosure:'Disclosure',
  checklist:{},counts:{linkedin:19,x:[{weighted_length:10},{weighted_length:11}]},warnings:[],ready:false,
}};
const controller = createPublishingController({state,announce: text=>notices.push(text),
  showError: error => {throw error;}, renderMarkdown: (element, text) => {element.textContent = text;},
  api: async path => path.endsWith('/packages') ? {packages:[{id:value.id,revision_id:state.article.id,created_at:'2026-11-10T00:00:00Z'}]}
    : path.endsWith('/publications') ? {publications:[]} : value,
});
await controller.render();
await $('copy-publication-title').fire();
assert.equal(writes.at(-1).text,'Saved title');
await $('copy-publication-formatted').fire();
let formats = writes.at(-1).items[0].types;
assert.equal(await formats['text/html'].text(),value.data.medium_body_html);
assert.equal(await formats['text/plain'].text(),value.data.medium_body_text);
await $('saved-x-posts').children[1].children[2].fire();
assert.equal(writes.at(-1).text,'Second post');
await $('channel-x-button').fire();
assert.equal($('channel-x').hidden,false);
assert.equal($('channel-medium').hidden,true);
globalThis.ClipboardItem = undefined;
await $('copy-publication-formatted').fire();
assert.equal(writes.at(-1).text,value.data.medium_body_text);
assert.match(notices.at(-1),/plain text/);
const count = writes.length;
for (const flag of ['publicationDirty','editorDirty','saving']) {
  state[flag] = true; controller.update();
  assert.equal($('copy-publication-title').disabled,true);
  assert.equal($('publication-download').hidden,true);
  await $('copy-publication-title').fire(); await $('copy-publication-formatted').fire();
  state[flag] = false;
}
assert.equal(writes.length,count);
state.article.content_hash='other-hash'; controller.update();
await $('copy-publication-title').fire(); assert.equal(writes.length,count);
state.article.content_hash='hash-a';
value.stale=true; controller.update();
await $('copy-publication-title').fire(); assert.equal(writes.length,count);
assert.equal($('publication-download').hidden,true);
value.stale=false;
clipboard.writeText = async () => {throw Error('permission denied');};
await $('copy-publication-article').fire();
assert.match(notices.at(-1),/Download the text package/);
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
