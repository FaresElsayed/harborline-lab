#!/usr/bin/env node
// Turns the ticket catalog (R/tracker/tickets/pNN_*.yaml) into the fixture the dev mock serves (VITE_MOCK=1).
//   npm run fixtures            reads ../tracker/tickets
//   npm run fixtures -- <dir>   reads another folder
// The output is committed so the Docker build, which does not copy tracker/, never needs the YAML.
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = path.resolve(process.argv[2] ?? path.join(here, '../../tracker/tickets'));
const out = path.join(here, '../src/mock/fixtures/tickets.generated.json');

const files = readdirSync(dir)
  .filter((f) => /^p\d\d.*\.ya?ml$/i.test(f))
  .sort();
if (!files.length) {
  console.error(`No pNN_*.yaml files in ${dir}`);
  process.exit(1);
}

const list = (v) => (Array.isArray(v) ? v.map(String) : v ? [String(v)] : []);
const text = (v) => (v == null ? '' : String(v).trim());

const projects = files.map((f) => {
  const doc = parse(readFileSync(path.join(dir, f), 'utf8'));
  const e = doc.epic ?? {};
  return {
    project: Number(doc.project),
    epic: {
      id: e.id,
      summary: text(e.summary),
      department: text(e.department),
      reporter: e.reporter,
      priority: e.priority ?? 'High',
      labels: list(e.labels),
      description: text(e.description),
      dashboards: list(e.dashboards),
    },
    stories: (doc.stories ?? []).map((s) => ({
      id: s.id,
      type: 'Story',
      summary: text(s.summary),
      reporter: s.reporter ?? e.reporter,
      priority: s.priority ?? 'Medium',
      story_points: Number(s.story_points ?? 0),
      sprint: Number(s.sprint ?? (Number(s.due_day) <= 10 ? 1 : 2)),
      due_day: Number(s.due_day),
      components: list(s.components),
      labels: list(s.labels),
      description: text(s.description),
      acceptance_criteria: list(s.acceptance_criteria),
      definition_of_done: list(s.definition_of_done),
      depends_on: list(s.depends_on),
      attachments: list(s.attachments),
      tasks: (s.tasks ?? []).map((t) => ({
        id: t.id,
        type: ['Bug', 'Spike'].includes(t.type) ? t.type : 'Task',
        summary: text(t.summary),
        estimate_hours: Number(t.estimate_hours ?? 0),
        due_day: Number(t.due_day),
        priority: t.priority ?? 'Medium',
        components: list(t.components),
        labels: list(t.labels),
        description: text(t.description),
        acceptance_criteria: list(t.acceptance_criteria),
        definition_of_done: list(t.definition_of_done),
        depends_on: list(t.depends_on),
      })),
    })),
  };
});

mkdirSync(path.dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify(projects) + '\n');
const stories = projects.reduce((n, p) => n + p.stories.length, 0);
const tasks = projects.reduce((n, p) => n + p.stories.reduce((m, s) => m + s.tasks.length, 0), 0);
const hours = projects.map((p) => p.stories.reduce((m, s) => m + s.tasks.reduce((h, t) => h + t.estimate_hours, 0), 0));
console.log(`${files.length} projects, ${stories} stories, ${tasks} tasks -> ${path.relative(process.cwd(), out)}`);
console.log(`hours per project: ${hours.join(', ')}`);
