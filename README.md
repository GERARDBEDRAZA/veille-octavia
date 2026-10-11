# Veille Octavia Exclusive Selection

Page statique (`index.html`) alimentée par `data.json`, mis à jour par `crawler.py` via GitHub Actions.
Le bouton « Actualiser la recherche » déclenche le workflow par l'intermédiaire d'un Cloudflare Worker (le token GitHub n'est jamais dans la page).

## Mise en ligne (une seule fois)

1. **Dépôt** : créer `GERARDBEDRAZA/veille-octavia` (branche `main`) et y déposer tout le contenu de ce dossier.
2. **GitHub Pages** : Settings → Pages → Deploy from a branch → `main` / racine.
   URL attendue : `https://gerardbedraza.github.io/veille-octavia/`.
3. **Workflow** : onglet Actions → autoriser les workflows ; Settings → Actions → General → Workflow permissions → *Read and write*.
   Un premier passage manuel : Actions → « Actualiser la recherche » → Run workflow.
4. **Token pour le bouton** : GitHub → Settings → Developer settings → Fine-grained tokens, limité au seul dépôt `veille-octavia`, permission *Actions : Read and write*.
5. **Worker** : dans `worker/`, `npx wrangler deploy`, puis `npx wrangler secret put GH_TOKEN` (coller le token).
   Vérifier `GH_REPO` et `ORIGIN` dans `wrangler.toml` (ORIGIN = `https://gerardbedraza.github.io`, sans chemin).
6. **Page** : dans `index.html`, renseigner `const API = "https://veille-octavia-refresh.<compte>.workers.dev";`.

Tant que `API` est vide, le bouton relit simplement `data.json`.

## Test local du crawler

```
python crawler.py --data data.json --only jičín      # un seul concessionnaire
python crawler.py --data data.json --max-sites 5
```

## Limites connues

- Les stocks chargés par JavaScript ne sont pas lus (prévoir Playwright dans le workflow si nécessaire).
- Le niveau « portail Škoda » repose sur les sous-domaines `*.skoda-auto.cz` repérés dans le journal ; les portails Škoda Plus, Autobazar et DasWeltAuto ne sont pas encore interrogés par le crawler.
- Le tri liftback/Combi s'appuie sur le 4e caractère du VIN (A = liftback, J = Combi), déduit des VIN observés.
- Les équipements (DCC, Canton, attelage, tri-zone) sont détectés dans le texte et restent à confirmer par VIN auprès du vendeur.
- Les erreurs 466/502 du premier passage n'ont pas été diagnostiquées : le premier run GitHub Actions montrera si elles persistent.
