# Annonce HACF — v3.8.0 / v3.8.1 (à poster dans le fil du tuto)

> Fil : https://forum.hacf.fr/t/tuto-controler-un-thermostat-daikin-madoka-brc1h-via-bluetooth-avec-home-assistant-integration-custom-esphome/75688

---

## 🔌 Daikin Madoka v3.8.1 — un thermostat ne peut plus devenir irrécupérable

Bonjour à tous,

La [v3.8.1](https://github.com/dasimon135/daikin_madoka/releases/tag/v3.8.1) vient de sortir, et c'est de loin la plus grosse release de fiabilité : toute la **couche connexion / appairage / récupération** a été réécrite après une revue complète. Trois pannes bien réelles sur ma propre installation (4 thermostats, 4 proxys) ont déclenché le chantier, et tout ce qui suit a été validé sur ce matériel. 😅

### 🩺 Ce qui n'allait pas

Un thermostat dont le bond Bluetooth était **parfaitement sain** s'est fait accuser d'avoir *refusé l'appairage*, et mis en quarantaine. Deux autres se sont retrouvés en `setup_retry` — un état où **toutes les entités disparaissent**, y compris le bouton **Reconnecter** que la notification de l'intégration vous demande justement d'appuyer. Et aucun des deux ne pouvait être ré-appairé par le moindre chemin documenté : le budget de 60 s censé vous laisser le temps d'aller jusqu'au thermostat était imbriqué dans un budget de connexion de 30 s, donc silencieusement annulé vers 28 s. 🙃

Trois bugs distincts, une seule cause de fond : le code n'avait jamais énoncé la règle qui gouverne tout ça.

> Un bond est mémorisé **par proxy**. Sur un chemin qui a déjà un bond, personne n'a besoin de toucher au thermostat — donc un **timeout** d'appairage n'y signifie que de la congestion, jamais un bond perdu. Seul un **refus explicite** prouve qu'un humain est nécessaire.

Faute de l'avoir écrite, l'intégration avait empilé quatre garde-fous qui l'approximaient chacun à moitié, ajoutés un par un après chaque incident.

### ✨ Ce qui change

- 🚫 **Un timeout n'est plus pris pour un refus.** Seul un refus explicite met un thermostat en quarantaine. Une série de timeouts ralentit fortement les tentatives et lève un simple avertissement disant que l'appairage n'aboutit pas — sans accuser le thermostat de quoi que ce soit.
- ⏱️ **Les budgets d'appairage sont dimensionnés selon le nombre de proxys** qui seront essayés, pour qu'un verdict puisse réellement se former. Avant, avec deux proxys ou plus à portée, aucun verdict ne pouvait *jamais* aboutir.
- 🩹 **Un thermostat configuré se charge toujours**, en mode dégradé s'il ne peut pas se connecter, au lieu de disparaître en `setup_retry`. Ses entités existent et affichent `indisponible` — dont le bouton **Reconnecter**.
- 🔑 **Un flux de ré-appairage** apparaît sur l'entrée de l'intégration après un vrai refus, avec un bouton **Réparer**, et il fonctionne **même quand aucune entité n'est disponible**. Il vous dit s'il a réussi ou échoué, au lieu de vous laisser deviner.
- 🔎 **Nouveau capteur « État de la connexion »** : `connecté`, `nouvelle tentative`, `appairage qui n'aboutit pas`, `appairage requis`, `n'émet plus`. Il reste disponible quand le lien est coupé — tout comme les capteurs de puissance du signal et de source de connexion, qui lisent des données côté Home Assistant et n'ont jamais eu besoin du thermostat.
- 🎰 **Les proxys sans slot de connexion libre sont essayés en dernier**, pour qu'un proxy saturé cesse de bloquer un thermostat pendant que les autres tournent à vide.
- ⚡ **Vos actions prennent effet immédiatement** (v3.8.1) : appuyer sur Reconnecter ou valider le ré-appairage annule le ralentissement et retente aussitôt, au lieu de laisser votre aller-retour jusqu'au thermostat sans effet visible pendant un quart d'heure.
- 🧾 Les bonds sont enregistrés dès que l'appairage réussit, un proxy qui refuse systématiquement est écarté (jamais le dernier), et renommer un thermostat n'efface plus sa liste de proxys appairés.
- ⚙️ Nécessite **pymadoka-ng 0.3.10** (installé automatiquement), qui indique désormais *pourquoi* l'appairage a échoué au lieu de laisser l'intégration deviner.
- 🃏 **Carte Madoka 0.7.1** : la carte n'annonce plus une reconnexion réussie qui n'a jamais eu lieu — elle affiche un vrai état d'attente, et une erreur visible si l'appel échoue. Videz le cache du navigateur une fois.

Côté tests, on passe de 102 à 223. Et le composant ESPHome est maintenant **réellement compilé en CI** à chaque changement — ça n'avait jamais été le cas.

### ⚠️ Le piège qui m'a coûté une journée — à connaître absolument

Un répondeur d'appairage se déclare **par couple (proxy, thermostat)**. Mes quatre proxys ne listaient chacun que les deux thermostats pour lesquels ils avaient été créés — sauf que les quatre sont *actifs* et à portée des quatre thermostats. Home Assistant envoyait donc tranquillement une tentative d'appairage via un proxy qui **n'avait aucun répondeur pour cette adresse**. Personne ne répond à la comparaison numérique, la tentative ne peut que expirer, et **confirmer au thermostat n'y change rien**. 😤

Si vous avez plusieurs proxys actifs : **chaque proxy actif doit avoir un répondeur pour chaque thermostat qu'il peut atteindre** (ou passez-le en `bluetooth_proxy: active: false`). Et dimensionnez `esp32_ble: max_connections` à **3 + nombre de répondeurs** — trop bas, les répondeurs échouent **silencieusement** au démarrage et vous reproduisez exactement le même symptôme. Tout est détaillé dans la [doc ESPHome](https://github.com/dasimon135/daikin_madoka/blob/main/docs/esphome-proxy.md).

### 💥 Changement cassant — composant ESPHome uniquement

La double consigne était codée en dur : l'entité climate de l'ESP32 exposait toujours deux températures, quel que soit le réglage du thermostat. C'est désormais l'option `dual_setpoint:`, **avec une consigne unique par défaut**. Si vous tenez à la double consigne — ou si vous appelez `climate.set_temperature` avec `target_temp_low`/`target_temp_high` sur une entité Madoka ESPHome — ajoutez `dual_setpoint: true` dans le bloc climate et recompilez. **L'intégration Home Assistant n'est pas concernée** : elle basculait déjà toute seule.

### ⬆️ Mise à jour

Via HACS (dépôt personnalisé `dasimon135/daikin_madoka` si ce n'est pas déjà fait) puis redémarrage. Les `entity_id` et l'historique sont conservés, et il n'y a rien à reconfigurer. Le redémarrage est un peu plus long que d'habitude : Home Assistant installe la nouvelle version de la bibliothèque.

Retours bienvenus, ici ou sur [GitHub](https://github.com/dasimon135/daikin_madoka/issues) !

Bonne clim' à tous ! ❄️🔥
