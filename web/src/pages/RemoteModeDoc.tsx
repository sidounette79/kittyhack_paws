import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import TopBanner from "../components/TopBanner";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-5">
      <h2 className="mb-2 text-base font-semibold">{title}</h2>
      <div className="space-y-2 text-sm text-white/80">{children}</div>
    </section>
  );
}

// 08.10, Sid ("pour la doc, tu peux créer une page et la conserver?"):
// French-translated React port of doc/remote-mode.md (the 2nd of the 2
// sections deliberately left on the old Shiny Configuration page during its
// cutover - see Configuration.tsx's Info section comment). The other one
// (the self-update/apply flow) stays unported on purpose - Sid: "je vais
// jamais reprendre les màj de la page officielle, vu qu'on a tout forké".
export default function RemoteModeDocPage() {
  usePageTitle("Mode distant");
  const navigate = useNavigate();

  useBackButton(
    () => false,
    () => navigate("/configuration")
  );

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-4 flex items-center gap-3">
          <button
            onClick={() => navigate("/configuration")}
            aria-label="Retour"
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
          >
            ‹
          </button>
          <div className="text-lg font-semibold">Mode distant (remote-mode)</div>
        </div>

        <p className="mb-5 text-sm text-white/70">
          Le mode distant est une fonctionnalité optionnelle qui répartit Kittyhack sur deux appareils :
        </p>
        <ul className="mb-5 list-disc space-y-1 pl-5 text-sm text-white/80">
          <li>
            <strong>Kittyflap</strong> : gère toujours le matériel local (capteurs, RFID, verrous).
          </li>
          <li>
            <strong>Appareil distant</strong> (PC/VM Linux séparé) : fait tourner l'interface web et
            l'inférence IA.
          </li>
        </ul>
        <p className="mb-6 text-sm text-white/70">
          Cela réduit la charge sur la Kittyflap et, selon le matériel utilisé, permet des fréquences
          d'images bien plus élevées pour la détection des chats et des proies. Des modèles plus complexes,
          incapables de tourner sur le Raspberry Pi 4 relativement peu puissant de la Kittyflap, peuvent
          aussi être utilisés.
        </p>

        <Block title="Schémas">
          <div className="space-y-4">
            <div>
              <p className="mb-1 text-xs text-white/65">Architecture : qui parle à qui ?</p>
              <img
                src="/diagrams/remote-mode-architecture.svg"
                alt="Architecture du mode distant"
                className="w-full rounded-lg bg-white/90 p-2"
              />
            </div>
            <div>
              <p className="mb-1 text-xs text-white/65">
                Prise de contrôle : que se passe-t-il quand le distant se connecte
              </p>
              <img
                src="/diagrams/remote-mode-control-takeover.svg"
                alt="Prise de contrôle du mode distant"
                className="w-full rounded-lg bg-white/90 p-2"
              />
            </div>
            <div>
              <p className="mb-1 text-xs text-white/65">Récupération après perte de connexion</p>
              <img
                src="/diagrams/remote-mode-connection-loss.svg"
                alt="Récupération après perte de connexion"
                className="w-full rounded-lg bg-white/90 p-2"
              />
            </div>
          </div>
        </Block>

        <Block title="Qui fait quoi ?">
          <p className="font-medium text-white/90">Sur la Kittyflap</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>Kittyhack tourne normalement par défaut.</li>
            <li>
              Quand un appareil distant prend le contrôle, la Kittyflap affiche une page d'information à la
              place de l'interface habituelle.
            </li>
            <li>Si la connexion est perdue, la Kittyflap reprend automatiquement la main en secours.</li>
          </ul>
          <p className="mt-3 font-medium text-white/90">Sur l'appareil distant</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>Fait tourner l'interface web de Kittyhack.</li>
            <li>Se connecte à la Kittyflap et la contrôle à distance.</li>
          </ul>
        </Block>

        <Block title="Installation">
          <ol className="list-decimal space-y-2 pl-5">
            <li>Installer/mettre à jour Kittyhack sur la Kittyflap comme d'habitude.</li>
            <li>
              Installer Kittyhack sur l'appareil distant et choisir « remote-mode » lors de l'installation.
              <pre className="mt-1 overflow-x-auto rounded-md bg-black/40 p-2 text-xs text-white/70">
                sudo curl -sSL
                https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o
                /tmp/kittyhack-setup.sh &amp;&amp; sudo chmod +x /tmp/kittyhack-setup.sh &amp;&amp; sudo
                /tmp/kittyhack-setup.sh &amp;&amp; sudo rm /tmp/kittyhack-setup.sh
              </pre>
            </li>
            <li>Dans l'interface web de l'appareil distant, entrer l'adresse IP de la Kittyflap.</li>
          </ol>
          <p className="mt-2">
            Ouvrir l'interface distante dans le navigateur :{" "}
            <code className="rounded bg-black/40 px-1.5 py-0.5 text-xs">http://&lt;IP-appareil-distant&gt;/</code>
          </p>
        </Block>

        <Block title="À propos de la synchronisation initiale">
          <p>
            Pendant l'installation, des données peuvent être transférées de la Kittyflap vers l'appareil
            distant : modèles, photos, base de données, configuration, données Label Studio.
          </p>
          <p>
            Cette synchronisation n'est disponible <strong>qu'à l'installation</strong>. Une
            resynchronisation manuelle ultérieure n'est actuellement <strong>pas</strong> disponible.
          </p>
        </Block>

        <Block title="Où sont stockées les données ?">
          <p>Quand la Kittyflap est contrôlée par un appareil distant :</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>les nouveaux événements sont enregistrés uniquement sur l'appareil distant</li>
            <li>les changements de configuration sont enregistrés uniquement sur l'appareil distant</li>
          </ul>
          <p>
            Si l'appareil distant se déconnecte, la Kittyflap reprend la main localement en secours — en
            utilisant les réglages stockés sur la Kittyflap elle-même.
          </p>
          <p className="font-medium text-amber-300">
            En résumé : après un certain temps, l'appareil distant et la Kittyflap auront des données
            différentes (événements et configuration) !
          </p>
          <p>
            La connexion distante peut être interrompue depuis l'interface web de l'appareil distant. Après
            quoi, la configuration redevient possible depuis l'interface web de la Kittyflap.
          </p>
        </Block>

        <Block title="Que se passe-t-il si la connexion est perdue ?">
          <ul className="list-disc space-y-1 pl-5">
            <li>La Kittyflap relâche le contrôle distant.</li>
            <li>Les actions de sécurité sont terminées proprement.</li>
            <li>La Kittyflap revient à un fonctionnement local autonome.</li>
            <li>L'appareil distant tentera automatiquement de se reconnecter plus tard.</li>
          </ul>
        </Block>

        <Block title="Prérequis logiciels et matériels">
          <p className="font-medium text-white/90">Appareil distant</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>Debian ou Ubuntu (idéalement un système dédié exclusivement à Kittyhack)</li>
            <li>CPU AMD64 (x86_64) avec au moins 4 cœurs</li>
            <li>Au moins 2 Go de RAM</li>
          </ul>
          <p className="mt-3 font-medium text-white/90">Réseau</p>
          <ul className="list-disc space-y-1 pl-5">
            <li>L'appareil distant doit pouvoir joindre la Kittyflap sur le réseau local.</li>
            <li>
              La Kittyflap doit être joignable sur les ports <code className="rounded bg-black/40 px-1">80</code> et{" "}
              <code className="rounded bg-black/40 px-1">8888</code>.
            </li>
          </ul>
        </Block>
      </div>
      <BottomNav active="configuration" />
    </>
  );
}
