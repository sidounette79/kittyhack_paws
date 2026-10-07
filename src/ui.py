from shiny import ui
from pathlib import Path
from faicons import icon_svg
from src.mode import is_remote_mode
from src.baseconfig import CONFIG, set_language

js_file = Path(__file__).parent.parent / "www" / "app.js"
server_ui_js_file = Path(__file__).parent.parent / "www" / "server-ui.js"
event_modal_js_file = Path(__file__).parent.parent / "www" / "event-modal.js"
webpush_client_js_file = Path(__file__).parent.parent / "www" / "webpush-client.js"
fcm_native_client_js_file = Path(__file__).parent.parent / "www" / "fcm-native-client.js"
android_back_button_js_file = Path(__file__).parent.parent / "www" / "android-back-button.js"
watchdog_cams_js_file = Path(__file__).parent.parent / "www" / "watchdog-cams-client.js"
photos_correction_js_file = Path(__file__).parent.parent / "www" / "photos-correction-client.js"
journey_client_js_file = Path(__file__).parent.parent / "www" / "journey-client.js"
css_file = Path(__file__).parent.parent / "www" / "styles.css"
logo_navbar_file = Path(__file__).parent.parent / "www" / "logo-navbar.png"

try:
    _asset_version = str(int(css_file.stat().st_mtime))
except Exception:
    _asset_version = "0"

try:
    _js_version = str(int(js_file.stat().st_mtime))
except Exception:
    _js_version = "0"

try:
    _server_ui_js_version = str(int(server_ui_js_file.stat().st_mtime))
except Exception:
    _server_ui_js_version = "0"

try:
    _event_modal_js_version = str(int(event_modal_js_file.stat().st_mtime))
except Exception:
    _event_modal_js_version = "0"

try:
    _logo_navbar_version = str(int(logo_navbar_file.stat().st_mtime))
except Exception:
    _logo_navbar_version = "0"

try:
    _webpush_client_js_version = str(int(webpush_client_js_file.stat().st_mtime))
except Exception:
    _webpush_client_js_version = "0"

try:
    _fcm_native_client_js_version = str(int(fcm_native_client_js_file.stat().st_mtime))
except Exception:
    _fcm_native_client_js_version = "0"

try:
    _android_back_button_js_version = str(int(android_back_button_js_file.stat().st_mtime))
except Exception:
    _android_back_button_js_version = "0"

try:
    _watchdog_cams_js_version = str(int(watchdog_cams_js_file.stat().st_mtime))
except Exception:
    _watchdog_cams_js_version = "0"

try:
    _photos_correction_js_version = str(int(photos_correction_js_file.stat().st_mtime))
except Exception:
    _photos_correction_js_version = "0"

try:
    _journey_client_js_version = str(int(journey_client_js_file.stat().st_mtime))
except Exception:
    _journey_client_js_version = "0"

# Prepare gettext for translations
_ = set_language(CONFIG['LANGUAGE'])

# Build navigation panels (conditionally add WLAN config for target-mode)
_nav_items = [
    ui.nav_panel(
        _("Presence"),
        ui.output_ui("ui_presence"),
        value="presence",
    ),
    ui.nav_panel(
        _("Live view"),
        ui.output_ui("ui_live_view"),
        ui.output_ui("ui_live_view_footer"),
        ui.output_ui("ui_last_events"),
        ui.output_ui("ui_outdoor_cameras"),
        value="live-view",
    ),
    ui.nav_panel(
        _("Journey"),
        ui.output_ui("ui_journey_date"),
        ui.output_ui("ui_journey_timeline"),
        ui.br(),
        value="journey",
    ),
    # 06.10, Sid: a second, simpler tab - same flap events as "Journey"
    # (above, labeled "Parcours" again - see the fr.po msgstr change), but
    # without the correlated watchdog-camera capture grid. Own nav_panel
    # (not just a toggle on Journey) because she wants both reachable
    # separately, eventually from distinct icons on a future bottom nav bar
    # (clock for this one, a route/path icon for Parcours).
    ui.nav_panel(
        _("Chronology"),
        ui.output_ui("ui_chronology_date"),
        ui.output_ui("ui_chronology_timeline"),
        ui.br(),
        value="chronology",
    ),
    ui.nav_panel(
        _("Pictures"),
        ui.output_ui("ui_photos_date"),
        ui.output_ui("ui_photos_events"),
        ui.br(),
        value="pictures",
    ),
    # 05.10, Sid (page merge, step 2 - "supprimer ensuite la page gerer
    # les chats... tu peux la garder dans le code, juste qu'elle ne
    # s'affiche plus dans l'app"): hidden from the nav bar - per-cat
    # settings now live inline on each cat's Presence detail page
    # (build_cat_settings_card(), reused from cats.py), "Add a new cat"
    # moved into Configuration. ui_manage_cats()/register_cats() stay
    # fully registered and working under the hood, just not mounted
    # anywhere right now - uncomment this nav_panel to bring the old
    # all-cats list page back if needed.
    # ui.nav_panel(
    #     _("Manage cats"),
    #     ui.output_ui("ui_manage_cats"),
    #     ui.br(),
    #     value="manage-cats",
    # ),
    ui.nav_panel(
        _("AI Training"),
        ui.output_ui("ui_ai_training"),
        ui.br(),
        value="ai-training",
    ),
    ui.nav_panel(
        _("Configuration"),
        ui.output_ui("ui_configuration"),
        value="configuration",
    ),
]

if not is_remote_mode():
    _nav_items.append(
        ui.nav_panel(
            _("WLAN Configuration"),
            ui.output_ui("ui_wlan_configured_connections"),
            ui.output_ui("ui_wlan_available_networks"),
            value="wlan-configuration",
        )
    )

# 07.09, Sid: both moved out of the navbar title (theme toggle sat
# pixel-close to the remote-mode disconnect button there, easy to hit by
# mistake - "Disconnect" looks like a power icon in compact mode and drops
# the remote link, handing control back to the flap locally) and into the
# nav menu itself, which already collapses into the hamburger on narrow
# screens. Both in one nav_control, spaced to opposite ends of the same row
# (theme toggle left, disconnect right) rather than stacked as two rows.
# Same button id as before, so server-ui.js's existing click handler still
# finds theme_toggle_button with no changes needed there.
_nav_items.append(
    ui.nav_control(
        ui.tags.div(
            {"class": "d-flex justify-content-between align-items-center w-100"},
            ui.tags.button(
                {
                    "id": "theme_toggle_button",
                    "type": "button",
                    "class": "btn btn-sm btn-outline-secondary theme-toggle-btn",
                    "aria-label": _("Toggle theme"),
                    "title": _("Toggle theme")
                },
                _("Theme: Auto")
            ),
            ui.tags.span(
                {"class": "remote-nav-controls-wrapper"},
                ui.output_ui("ui_remote_connection_badge"),
            ),
        ),
    )
)

# 07.10, Sid ("le menu principal deroulant ne se referme pas... ou encore
# mieux, une barre de navigation fixe en bas avec des icones"): a real fixed
# bottom nav bar for mobile, replacing the need to dig into the hamburger
# menu for the tabs she actually uses daily. Reuses app.js's existing
# activateTab()/getActiveTabValue() pattern (already used for swipe
# gestures) via data-tab-value, rendered server-side with the same
# icon_svg() every other icon in this app uses - only the click-wiring and
# active-state sync live in JS. Hidden above a mobile breakpoint (styles.css)
# so the desktop top navbar stays the only nav there.
_BOTTOM_NAV_MAIN_ITEMS = [
    ("presence", "paw", lambda: _("Presence")),
    ("live-view", "tv", lambda: _("Live view")),
    ("chronology", "clock", lambda: _("Chronology")),
    ("journey", "route", lambda: _("Journey")),
]

_BOTTOM_NAV_MORE_ITEMS = [
    ("pictures", lambda: _("Pictures")),
    ("ai-training", lambda: _("AI Training")),
    ("configuration", lambda: _("Configuration")),
]
if not is_remote_mode():
    _BOTTOM_NAV_MORE_ITEMS.append(("wlan-configuration", lambda: _("WLAN Configuration")))


def _bottom_nav_bar():
    main_buttons = [
        ui.tags.button(
            icon_svg(icon_name, margin_left="0", margin_right="0"),
            ui.tags.span(label_fn(), class_="kh-bottom-nav-label"),
            type="button",
            class_="kh-bottom-nav-item",
            **{"data-tab-value": tab_value},
        )
        for tab_value, icon_name, label_fn in _BOTTOM_NAV_MAIN_ITEMS
    ]
    more_button = ui.tags.button(
        icon_svg("bars", margin_left="0", margin_right="0"),
        ui.tags.span(_("More"), class_="kh-bottom-nav-label"),
        type="button",
        id="kh_bottom_nav_more_btn",
        class_="kh-bottom-nav-item kh-bottom-nav-more",
    )
    more_menu_items = [
        ui.tags.button(
            label_fn(),
            type="button",
            class_="kh-bottom-nav-more-item",
            **{"data-tab-value": tab_value},
        )
        for tab_value, label_fn in _BOTTOM_NAV_MORE_ITEMS
    ]
    return ui.tags.div(
        ui.tags.nav(
            *main_buttons,
            more_button,
            id="kh_bottom_nav",
            class_="kh-bottom-nav",
        ),
        ui.tags.div(
            *more_menu_items,
            id="kh_bottom_nav_more_menu",
            class_="kh-bottom-nav-more-menu",
            hidden=True,
        ),
    )


# the main kittyhack ui
app_ui = ui.page_fillable(
    ui.tags.script(src=f"app.js?v={_js_version}", defer=True),
    ui.tags.script(src=f"server-ui.js?v={_server_ui_js_version}", defer=True),
    ui.tags.script(src=f"event-modal.js?v={_event_modal_js_version}", defer=True),
    ui.tags.script(src=f"webpush-client.js?v={_webpush_client_js_version}", defer=True),
    ui.tags.script(src=f"fcm-native-client.js?v={_fcm_native_client_js_version}", defer=True),
    ui.tags.script(src=f"android-back-button.js?v={_android_back_button_js_version}", defer=True),
    ui.tags.script(src=f"watchdog-cams-client.js?v={_watchdog_cams_js_version}", defer=True),
    ui.tags.script(src=f"photos-correction-client.js?v={_photos_correction_js_version}", defer=True),
    ui.tags.script(src=f"journey-client.js?v={_journey_client_js_version}", defer=True),
    ui.tags.link(rel="stylesheet", href=f"styles.css?v={_asset_version}"),
    ui.head_content(
        # 03.10, Sid: sans ca, le navigateur (detection de date native, ou
        # feature de traduction automatique meme a priori inutile puisque
        # la page est deja en francais) peut reinterpreter/reformater du
        # texte brut qui ressemble a une date - vu en vrai: "29.09" devenu
        # "29 mai / 2009" et le texte juste a cote corrompu ("me" -> "Moi").
        # Les deux balises ci-dessous desactivent respectivement la
        # detection de date native et la traduction automatique de la page.
        ui.tags.meta(name="format-detection", content="date=no, telephone=no, address=no, email=no"),
        ui.tags.meta(name="google", content="notranslate"),
        ui.tags.meta(name="theme-color", content="#FFFFFF"),
        ui.tags.link(rel="manifest", href="manifest.json"),
        ui.tags.link(rel="icon", type="image/png", sizes="64x64", href="favicon-64x64.png?v=8"),
        ui.tags.link(rel="icon", type="image/png", sizes="48x48", href="favicon-48x48.png?v=8"),
        ui.tags.link(rel="icon", type="image/png", sizes="32x32", href="favicon-32x32.png?v=8"),
        ui.tags.link(rel="icon", type="image/png", sizes="16x16", href="favicon-16x16.png?v=8"),
        ui.tags.link(rel="apple-touch-icon", sizes="180x180", href="apple-touch-icon.png?v=8"),
        ui.tags.link(rel="icon", type="image/x-icon", href="favicon.ico?v=8"),
    ),
    ui.navset_bar(
        *_nav_items,
        id="main_nav",
        title=ui.tags.div(
            {
                "class": "d-flex align-items-center gap-2 flex-nowrap navbar-title-wrap",
            },
            ui.tags.img(
                src=f"logo-navbar.png?v={_logo_navbar_version}",
                alt="Kittyhack",
                class_="navbar-logo-img",
            ),
        ),
        position="fixed-top",
        padding="4.5rem",
    ),
    _bottom_nav_bar(),
)
