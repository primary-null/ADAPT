"""Generate the public overview from the sanitized inventory; no device access.

Requires reportlab. Run from a source checkout with Python 3.11 or newer.
"""
from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Preformatted, KeepTogether

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor('#162822')
GREEN = colors.HexColor('#008750')
GRAY = colors.HexColor('#61736a')
STYLES = getSampleStyleSheet()
STYLES.add(ParagraphStyle(name='Copy', fontName='Helvetica', fontSize=10.4, leading=14.2, textColor=INK, spaceAfter=9))
STYLES.add(ParagraphStyle(name='Section', fontName='Helvetica-Bold', fontSize=23, leading=28, textColor=INK, spaceAfter=17))
STYLES.add(ParagraphStyle(name='Area', fontName='Helvetica-Bold', fontSize=12, leading=16, textColor=GREEN, spaceBefore=8, spaceAfter=5))
STYLES.add(ParagraphStyle(name='Label', fontName='Helvetica-Bold', fontSize=8, leading=12, textColor=GRAY, spaceAfter=5))
STYLES.add(ParagraphStyle(name='MonoSmall', fontName='Courier', fontSize=8, leading=11, textColor=INK, spaceAfter=10))


def p(text, style='Copy'):
    return Paragraph(escape(text).replace('\n', '<br/>'), STYLES[style])


def decorate(canvas, doc):
    width, height = doc.pagesize
    canvas.setStrokeColor(GREEN)
    canvas.setLineWidth(2)
    canvas.line(48, height - 37, width - 48, height - 37)
    canvas.setFont('Helvetica-Bold', 8)
    canvas.setFillColor(GRAY)
    canvas.drawString(48, height - 28, 'ADAPT / PUBLIC SOURCE OVERVIEW')
    canvas.setFont('Helvetica', 8)
    canvas.drawString(48, 29, '0.1.0 alpha  |  MIT  |  No Android screenshots')
    canvas.drawRightString(width - 48, 29, str(doc.page))


def main():
    inventory = json.loads((ROOT / 'docs/feature-inventory.json').read_text(encoding='utf-8'))
    counts = inventory['counts']
    output = ROOT / 'docs/ADAPT-overview.pdf'
    story = [Spacer(1, 30), p('ADAPT', 'Section'), p('Android Device Agent Provisioning Toolkit', 'Area'),
        p('One console for Android setup, inspection, repeatable testing and AI tool access.'), Spacer(1, 15),
        p('PUBLIC ALPHA / WINDOWS 11 AND DEBIAN-LINUX', 'Label'),
        p('ADAPT combines a Python command-line tool, a local stdio MCP server and an experimental Windows-native wireless DeX receiver. The native receiver is included as source and built manually; it is outside the Python wheel.'),
        p('The original ADAPT console retains the green-to-white-to-gray block identity. Rapid ADAPT is a focused option inside that console. Built-in help explains the full toolset and gives contextual tips that users can disable.'),
        Spacer(1, 15), p('A counted, bounded toolset', 'Area'),
        p(f"{counts['functional_areas']} reviewed functional areas; {counts['visible_leaf_commands']} visible commands in {counts['visible_groups_including_root']} groups including the root; {counts['mcp_tools']} MCP tools; {counts['catalog_tools']} catalog entries; {counts['developer_profiles']} recoverable Developer Options profiles."),
        p('These counts organize capabilities. They are not a claim that every command or Android version has passed physical acceptance.'),
        Spacer(1, 14), p('Explore the source', 'Area'),
        p('Start with README.md, docs/features.md, docs/command-reference.md and docs/architecture.md. The interactive showcase lives at docs/showcase/index.html. Its ten equal views contain nine generated illustrations and one rendered capture of actual offline terminal output.'),
        p('No phone screen images, private runtime logs, pairing information or owner-specific network diagrams are included in this overview.'), PageBreak()]
    groups = [(0,5), (5,10), (10,15), (15,19)]
    for index,(start,end) in enumerate(groups, 1):
        story += [p(f'Capabilities / {index} of 4', 'Section')]
        for area in inventory['functional_areas'][start:end]:
            story.append(KeepTogether([
                p(f"{area['id']}  {area['area']}", 'Area'),
                p(area['entry'], 'MonoSmall'),
                p(area['capability']),
                p('Boundary: ' + area['boundary']),
            ]))
        story.append(PageBreak())
    story += [p('Connections and ownership', 'Section'),
        p('The ADB daemon stays local to the host that owns the Android transport. Explicit serial targeting and shared-daemon preflight protect other active devices and sessions.'),
        Preformatted('''Human CLI                  AI client
    |                          |
    |                     stdio MCP
    +------------+-------------+
                 |
       ADAPT target / policy gates
                 |
        local ADB daemon :5037
                 |
        USB or authorized Wi-Fi
                 |
            Android device''', STYLES['MonoSmall']),
        p('Remote work uses an explicit SSH worker or a routed, authenticated device endpoint. Tailscale pairing and connection ports serve different listeners. An exit node routes traffic; subnet routes and access policy must still make the intended address reachable.'),
        Preformatted('''PC -- SSH stdio --> Worker ADAPT --> local ADB --> phone

PC -- Tailnet/subnet route --> phone TLS connection port
                              (pair separately first)

Phone DeX -- Wi-Fi Direct / RTSP / RTP --> native receiver
Phone     -- selected ADB transport --> scrcpy attachment''', STYLES['MonoSmall']),
        p('The native wireless receiver starts a real Samsung session through a receiver protocol. A generic Android virtual display is an app-testing surface and does not prove Samsung DeX activation.'),
        p('Developer profiles create exclusive identity-bound snapshots before changing allowlisted settings. Restore checks for conflicts and verifies values; crashes, disconnects or firmware changes can prevent recovery.'), PageBreak(),
        p('Evidence and open work', 'Section'),
        p('The public source is checked independently from the original private workspace. See docs/publication-checks.json for the current offline results and docs/validation.md for the evidence boundaries.'),
        p('Physical observations cover two Samsung phones at API 33 and API 37. Nine configured API baselines cover Android 10-17, including 12L, for planning and conservative capability evaluation. Intermediate versions, other OEMs and native Debian execution remain acceptance gaps.'),
        p('The native Windows receiver completed a wireless DeX session on Android 13. A controlled process replacement and one reconnect passed after a keyboard-focus correction. This does not certify every radio, Windows update, Android build, audio mode or long-running session.'),
        p('Direct Tailnet ADB remains unproven in the current test evidence. A strict TLS-port verifier encounters an empty property on the tested firmware. A successful private VPN/hotspot ADB path, pairing success and a reachable TCP port are separate facts.'),
        p('MCP is locally tested through the protocol SDK. External AI-client registration and live SSH-worker acceptance remain separate. The current input tools do not expose a secondary-display selector.'),
        p('Root, ROM and bootloader observations are indicators, not conclusive certification. Samsung Download mode differs from fastboot. ADAPT does not silently unlock, flash, root, build a ROM, or attach a debugger.'),
        p('The tool catalog manages Platform-Tools and optional scrcpy acquisition. Other catalog entries provide upstream acquisition guidance. Rapid ADAPT builds a workflow plan; it does not automatically run a whole testing journey.'),
        Spacer(1, 10), p('License and provenance', 'Area'),
        p('ADAPT source uses the MIT license in LICENSE. The native receiver retains its upstream MIT license and third-party notices. Optional tools and dependencies keep their own licenses and acquisition terms; the root license does not replace those terms.'),
        p('See THIRD-PARTY-NOTICES.md and native/windows-receiver/THIRD-PARTY-NOTICES.txt. The repository includes auditable helper source and build metadata. Generated media uses synthetic examples and actual offline CLI output only.')]
    doc = SimpleDocTemplate(str(output), pagesize=(612,792), rightMargin=48,leftMargin=48,topMargin=62,bottomMargin=52,title='ADAPT - Public Source Overview',author='ADAPT Contributors',subject='Capabilities, topology and validation boundaries', invariant=1)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    print(output.name)


if __name__ == '__main__':
    main()
