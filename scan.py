# JEV AI WiFi Analyzer
# Roni Bandini, 10/2026, GPL License
# ZimaBoard 2 + Awus036h

import argparse
import curses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
import requests
from scapy.all import AsyncSniffer, Dot11, Dot11Elt, RadioTap

apiKey = "" # enter your OpenRouter API key here
jevUrl = "https://openrouter.ai/api/alpha/decisions"
defaultModel = "~typesafe/jev-latest"
defaultChannels = list(range(1, 14))

def runCommand(cmdList):
    cmdResult = subprocess.run(cmdList, capture_output=True, text=True)
    if cmdResult.returncode != 0:
        errorDetail = (cmdResult.stderr or cmdResult.stdout).strip()
        raise RuntimeError(f"`{' '.join(cmdList)}` failed: {errorDetail}")
    return cmdResult.stdout

def setupMonitor(interfaceName, channelNum):
    for toolName in ("ip", "iw"):
        if shutil.which(toolName) is None:
            raise RuntimeError(f"`{toolName}` not found in PATH")
    if not os.path.isdir(f"/sys/class/net/{interfaceName}"):
        raise RuntimeError(f"Interface {interfaceName!r} does not exist")
    if shutil.which("nmcli"):
        subprocess.run(["nmcli", "device", "set", interfaceName, "managed", "no"], capture_output=True)
    runCommand(["ip", "link", "set", interfaceName, "down"])
    runCommand(["iw", "dev", interfaceName, "set", "type", "monitor"])
    runCommand(["ip", "link", "set", interfaceName, "up"])
    setChannel(interfaceName, channelNum)
    infoOutput = runCommand(["iw", "dev", interfaceName, "info"])
    if not re.search(r"^\s*type\s+monitor\b", infoOutput, re.M):
        raise RuntimeError(f"{interfaceName} is not in monitor mode:\n{infoOutput}")

def setChannel(interfaceName, channelNum):
    subprocess.run(["iw", "dev", interfaceName, "set", "channel", str(channelNum)], capture_output=True)

def restoreManaged(interfaceName):
    for cmdList in (
        ["ip", "link", "set", interfaceName, "down"],
        ["iw", "dev", interfaceName, "set", "type", "managed"],
        ["ip", "link", "set", interfaceName, "up"],
    ):
        subprocess.run(cmdList, capture_output=True)
    if shutil.which("nmcli"):
        subprocess.run(["nmcli", "device", "set", interfaceName, "managed", "yes"], capture_output=True)

class RunningStats:
    __slots__ = ("countNum", "meanVal", "minVal", "maxVal")
    
    def __init__(self):
        self.countNum = 0
        self.meanVal = 0.0
        self.minVal = None
        self.maxVal = None
        
    def pushValue(self, newValue):
        self.countNum += 1
        deltaVal = newValue - self.meanVal
        self.meanVal += deltaVal / self.countNum
        self.minVal = newValue if self.minVal is None else min(self.minVal, newValue)
        self.maxVal = newValue if self.maxVal is None else max(self.maxVal, newValue)

class AccessPointStats:
    def __init__(self, macAddress):
        self.macAddress = macAddress
        self.ssidName = "Hidden / Unknown"
        self.channelNum = None
        self.beaconCount = 0
        self.firstSeen = None
        self.lastSeen = None
        self.rssiStats = RunningStats()
        self.connectedClients = set()
        
    def addBeacon(self, packetData, timeStamp):
        if self.firstSeen is None:
            self.firstSeen = timeStamp
        self.lastSeen = max(timeStamp, self.lastSeen or timeStamp)
        self.beaconCount += 1
        if packetData.haslayer(Dot11Elt):
            elementData = packetData[Dot11Elt]
            while isinstance(elementData, Dot11Elt):
                if elementData.ID == 0 and elementData.info:
                    try:
                        self.ssidName = elementData.info.decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                elif elementData.ID == 3 and len(elementData.info) == 1:
                    self.channelNum = ord(elementData.info)
                elementData = elementData.payload
        if packetData.haslayer(RadioTap):
            signalValue = getattr(packetData[RadioTap], "dBm_AntSignal", None)
            try:
                if signalValue is not None:
                    self.rssiStats.pushValue(int(signalValue))
            except (TypeError, ValueError):
                pass
                
    def addClient(self, clientMac):
        if clientMac and clientMac != "ff:ff:ff:ff:ff:ff":
            self.connectedClients.add(clientMac.lower())
            
    def getSummary(self):
        durationTime = max((self.lastSeen or 1.0) - (self.firstSeen or 0.0), 1.0)
        return {
            "macAddress": self.macAddress,
            "ssidName": self.ssidName,
            "channelNum": self.channelNum,
            "beaconCount": self.beaconCount,
            "beaconsPerSecond": round(self.beaconCount / durationTime, 2),
            "rssiMean": round(self.rssiStats.meanVal, 1) if self.rssiStats.countNum else None,
            "rssiMin": self.rssiStats.minVal,
            "rssiMax": self.rssiStats.maxVal,
            "activeClients": len(self.connectedClients),
        }

jevQuestions = {
    "coverageQuality": {
        "type": "score",
        "instructions": "Evaluate the Wi-Fi signal coverage and heat map quality at this survey point based on RSSI",
        "criteria": [
            "Very Poor / Dead Zone",
            "Poor / Unreliable",
            "Acceptable / Basic Services",
            "Good / High Throughput",
            "Excellent / Strong Signal",
        ],
    },
    "congestionLevel": {
        "type": "choice",
        "instructions": "Analyze channel distribution and access point density to determine interference levels",
        "criteria": {
            "low": "Clean spectrum with minimal co-channel interference",
            "moderate": "Some overlapping networks but manageable",
            "high": "Crowded spectrum requiring immediate channel planning",
            "critical": "Severe interference causing packet loss and degraded performance",
        },
    },
    "optimizationAction": {
        "type": "choice",
        "instructions": "Select the most effective action to improve the heat map and network performance",
        "criteria": {
            "none": "No optimization required",
            "powerAdjustment": "Adjust AP transmit power to reduce overlap or fill dead zones",
            "channelPlan": "Reallocate AP channels to non-overlapping frequencies",
            "hardwareAddition": "Deploy additional access points to cover dead zones",
        },
    },
}

def analyzeSurveyData(surveyData, scannedChannels, targetModel):
    if not apiKey:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    systemState = {
        "context": "Passive Wi-Fi 2.4 GHz RF site-survey telemetry from the most recent measurement window",
        "channelsScanned": scannedChannels,
        "channelMetrics": surveyData["channelMetrics"],
        "accessPointsDetected": surveyData["accessPoints"],
        "measurementWindowSeconds": surveyData["windowSeconds"],
    }
    apiResponse = requests.post(
        jevUrl,
        headers={"Authorization": f"Bearer {apiKey}", "Content-Type": "application/json"},
        json={"model": targetModel, "state": systemState, "questions": jevQuestions},
        timeout=30,
    )
    if apiResponse.status_code != 200:
        raise RuntimeError(f"HTTP {apiResponse.status_code}: {apiResponse.text[:200]}")
    responseData = apiResponse.json()
    evalCost = (responseData.get("usage") or {}).get("cost") or 0.0
    return responseData["answers"], float(evalCost)

class SharedState:
    def __init__(self, cliArgs):
        self.cliArgs = cliArgs
        self.threadLock = threading.Lock()
        self.accessPoints = {}
        self.channelFrames = {ch: 0 for ch in cliArgs.channelsList}
        self.channelDwell = {ch: 0.0 for ch in cliArgs.channelsList}
        self.channelStartedAt = time.time()
        self.currentChannel = cliArgs.channelsList[0]
        self.totalFrames = 0
        self.totalCost = 0.0
        self.isAnalyzing = threading.Event()
        self.evalResults = {}
        self.lastExportMsg = ""
        self.windowStartedAt = time.time()

    def closeDwell(self):
        nowTime = time.time()
        if self.currentChannel in self.channelDwell:
            self.channelDwell[self.currentChannel] += max(0.0, nowTime - self.channelStartedAt)
        self.channelStartedAt = nowTime

    def resetWindow(self):
        self.accessPoints = {}
        self.channelFrames = {ch: 0 for ch in self.cliArgs.channelsList}
        self.channelDwell = {ch: 0.0 for ch in self.cliArgs.channelsList}
        self.totalFrames = 0
        self.windowStartedAt = time.time()
        self.channelStartedAt = time.time()

    def handlePacket(self, packetData):
        if not packetData.haslayer(Dot11):
            return

        dot11Layer = packetData[Dot11]
        timeStamp = float(packetData.time)

        with self.threadLock:
            self.channelFrames[self.currentChannel] += 1
            self.totalFrames += 1

            if dot11Layer.type == 0 and dot11Layer.subtype == 8:
                bssidMac = (dot11Layer.addr3 or "").lower()
                if not bssidMac:
                    return
                if bssidMac not in self.accessPoints:
                    self.accessPoints[bssidMac] = AccessPointStats(bssidMac)
                self.accessPoints[bssidMac].addBeacon(packetData, timeStamp)

            elif dot11Layer.type == 2:
                toDs = bool(dot11Layer.FCfield & 1)
                if toDs:
                    bssidMac = (dot11Layer.addr1 or "").lower()
                    clientMac = (dot11Layer.addr2 or "").lower()
                else:
                    bssidMac = (dot11Layer.addr2 or "").lower()
                    clientMac = (dot11Layer.addr1 or "").lower()
                if bssidMac in self.accessPoints:
                    self.accessPoints[bssidMac].addClient(clientMac)

    def buildWindowData(self, windowSeconds):
        channelMetrics = []
        for channelNum in self.cliArgs.channelsList:
            dwell = self.channelDwell.get(channelNum, 0.0)
            if channelNum == self.currentChannel:
                dwell += max(0.0, time.time() - self.channelStartedAt)
            frames = self.channelFrames.get(channelNum, 0)
            framesPerSecond = frames / dwell if dwell > 0.2 else 0.0
            aps = [ap.getSummary() for ap in self.accessPoints.values() if ap.channelNum == channelNum]
            rssis = [ap["rssiMean"] for ap in aps if ap["rssiMean"] is not None]
            channelMetrics.append({
                "channel": channelNum,
                "frames": frames,
                "dwellSeconds": round(dwell, 2),
                "framesPerSecond": round(framesPerSecond, 2),
                "accessPoints": len(aps),
                "averageRssi": round(sum(rssis) / len(rssis), 1) if rssis else None,
                "clients": sum(ap["activeClients"] for ap in aps),
            })

        return {
            "windowSeconds": round(windowSeconds, 1),
            "channelMetrics": channelMetrics,
            "accessPoints": [ap.getSummary() for ap in sorted(self.accessPoints.values(), key=lambda item: item.beaconCount, reverse=True)],
        }


def runChannelHopper(sharedState, interfaceName, channelsList, hopInterval, stopEvent):
    indexNum = 0
    while not stopEvent.is_set():
        targetChannel = channelsList[indexNum % len(channelsList)]
        try:
            setChannel(interfaceName, targetChannel)
        except RuntimeError as errorObj:
            with sharedState.threadLock:
                sharedState.lastExportMsg = str(errorObj)[:80]
        with sharedState.threadLock:
            sharedState.closeDwell()
            sharedState.currentChannel = targetChannel
            sharedState.channelStartedAt = time.time()
        indexNum += 1
        stopEvent.wait(hopInterval)


def saveSurveyExport(windowData, evalAnswers, cliArgs):
    os.makedirs(cliArgs.exportDir, exist_ok=True)
    timeStamp = datetime.now().strftime("%Y%m%d%H%M%S")
    exportPath = os.path.join(cliArgs.exportDir, f"siteSurvey{timeStamp}.json")
    exportData = {
        "timestamp": datetime.now().isoformat(),
        "windowSeconds": windowData["windowSeconds"],
        "channelsScanned": cliArgs.channelsList,
        "channelMetrics": windowData["channelMetrics"],
        "accessPoints": windowData["accessPoints"],
        "jevEvaluations": evalAnswers,
    }
    with open(exportPath, "w", encoding="utf-8") as fileHandle:
        json.dump(exportData, fileHandle, indent=2)
    return exportPath


def processAnalysis(sharedState, windowData, cliArgs):
    try:
        if not windowData["channelMetrics"]:
            return
        try:
            evalAnswers, evalCost = analyzeSurveyData(windowData, cliArgs.channelsList, cliArgs.targetModel)
            savedPath = saveSurveyExport(windowData, evalAnswers, cliArgs)
            with sharedState.threadLock:
                sharedState.totalCost += evalCost
                sharedState.evalResults = evalAnswers
                sharedState.lastExportMsg = f"Saved {os.path.basename(savedPath)}"
        except Exception as errorObj:
            with sharedState.threadLock:
                sharedState.lastExportMsg = f"Error {errorObj}"
    finally:
        sharedState.isAnalyzing.clear()


def renderDashboard(screenObj, sharedState, cliArgs, nextRunTime):
    screenObj.erase()
    maxHeight, maxWidth = screenObj.getmaxyx()

    def writeText(posY, posX, textStr, textAttr=0):
        if 0 <= posY < maxHeight and 0 <= posX < maxWidth:
            try:
                screenObj.addstr(posY, posX, str(textStr)[:max(0, maxWidth - posX - 1)], textAttr)
            except curses.error:
                pass

    with sharedState.threadLock:
        activeChannel = sharedState.currentChannel
        apList = list(sharedState.accessPoints.values())
        evalAnswers = dict(sharedState.evalResults)
        exportMsg = sharedState.lastExportMsg
        totalCost = sharedState.totalCost
        channelMetrics = []
        for channelNum in cliArgs.channelsList:
            dwell = sharedState.channelDwell.get(channelNum, 0.0)
            if channelNum == activeChannel:
                dwell += max(0.0, time.time() - sharedState.channelStartedAt)
            frames = sharedState.channelFrames.get(channelNum, 0)
            fps = frames / dwell if dwell > 0.2 else 0.0
            aps = [ap for ap in apList if ap.channelNum == channelNum]
            rssis = [ap.rssiStats.meanVal for ap in aps if ap.rssiStats.countNum]
            channelMetrics.append({
                "channel": channelNum,
                "frames": frames,
                "fps": fps,
                "aps": len(aps),
                "rssi": sum(rssis) / len(rssis) if rssis else None,
            })
        totalFrames = sharedState.totalFrames

    writeText(0, 2, "● ● ●", curses.A_DIM)
    title = "JEV WIFI ANALYZER - Roni Bandini 10/2026 - GPL License"
    writeText(0, max(2, (maxWidth - len(title)) // 2), title, curses.A_DIM)
    writeText(0, maxWidth - 18, f"LIVE · CH {activeChannel}", curses.A_DIM)
    writeText(2, 2, "W I - F I   2 . 4   G H Z   Z I M A B O A R D 2", curses.A_BOLD)
    writeText(3, 2, f"ALFA Awus036h · {cliArgs.interfaceName} · WINDOW {cliArgs.evalWindow}s · HOP {cliArgs.hopInterval:g}s", curses.A_DIM)
    writeText(4, 2, "─" * max(1, maxWidth - 4), curses.A_DIM)

    leftX = 2
    midX = maxWidth // 2
    rightX = int(maxWidth * 0.72)

    writeText(6, leftX, "CHANNEL HEAT · RECENT WINDOW", curses.A_BOLD)
    maxFps = max((item["fps"] for item in channelMetrics), default=0.0)
    barWidth = max(8, min(24, (midX - leftX) - 24))
    rowY = 8
    for item in channelMetrics:
        if rowY >= maxHeight - 10:
            break
        writeText(rowY, leftX, f"{item['channel']:>2}")
        filled = int((item["fps"] / maxFps) * barWidth) if maxFps > 0 else 0
        writeText(rowY, leftX + 4, "█" * filled + "·" * (barWidth - filled))
        writeText(rowY, leftX + 6 + barWidth, f"{item['fps']:5.1f}/s", curses.A_DIM)
        rowY += 1

    writeText(6, midX, "JEV ANALYSIS · LAST WINDOW", curses.A_BOLD)
    if evalAnswers:
        writeText(8, midX, f"COVERAGE      {evalAnswers.get('coverageQuality', {}).get('score', '--')}/4")
        writeText(9, midX, f"CONGESTION    {str(evalAnswers.get('congestionLevel', {}).get('choice', '--')).upper()}")
        writeText(10, midX, f"ACTION        {str(evalAnswers.get('optimizationAction', {}).get('choice', '--')).upper()}")
    elif sharedState.isAnalyzing.is_set():
        writeText(8, midX, "ANALYZING...", curses.A_BLINK)
    else:
        writeText(8, midX, "GATHERING TELEMETRY...")

    writeText(13, midX, "CHANNEL METRICS · LAST WINDOW", curses.A_BOLD)
    writeText(14, midX, "CH   FRAMES/S   APs   AVG RSSI", curses.A_DIM)
    metricsY = 15
    for item in channelMetrics:
        if metricsY >= maxHeight - 9:
            break
        rssiText = f"{item['rssi']:6.0f}" if item["rssi"] is not None else "    --"
        writeText(metricsY, midX, f"{item['channel']:>2}   {item['fps']:7.1f}   {item['aps']:>3}   {rssiText}")
        metricsY += 1

    writeText(6, rightX, "ACCESS POINTS · RECENT WINDOW", curses.A_BOLD)
    writeText(8, rightX, "BSSID              CH   RSSI   BEACONS   CLI", curses.A_DIM)
    listRow = 9
    for apObj in sorted(apList, key=lambda item: item.beaconCount, reverse=True)[:10]:
        if listRow >= maxHeight - 8:
            break
        apStats = apObj.getSummary()
        rssiStr = f"{apStats['rssiMean']:.0f}" if apStats["rssiMean"] is not None else "--"
        writeText(listRow, rightX, f"{apStats['macAddress']}  {apStats['channelNum'] or '?':>2}  {rssiStr:>5}  {apStats['beaconCount']:>7}  {apStats['activeClients']:>3}")
        listRow += 1

    bottomY = maxHeight - 6
    timeLeft = max(0.0, nextRunTime - time.time())
    progressPct = 1.0 - (timeLeft / cliArgs.evalWindow) if cliArgs.evalWindow > 0 else 0.0
    progressPct = max(0.0, min(1.0, progressPct))
    writeText(bottomY, leftX, f"FRAMES {totalFrames:06d}   APs {len(apList):03d}   JEV COST ${totalCost:.4f}", curses.A_BOLD)
    progressWidth = max(10, maxWidth - 24)
    filled = int(progressWidth * progressPct)
    writeText(bottomY + 1, leftX, "─" * progressWidth, curses.A_DIM)
    writeText(bottomY + 1, leftX, "█" * filled)
    writeText(bottomY + 1, leftX + progressWidth + 2, f"{int(progressPct * 100):3d}%", curses.A_DIM)
    writeText(bottomY + 3, leftX, "Recent-window metrics · ", curses.A_DIM)
    writeText(bottomY + 4, leftX, "SPACE pause   Q quit", curses.A_DIM)
    if exportMsg:
        writeText(bottomY + 4, max(2, maxWidth - len(exportMsg) - 2), exportMsg, curses.A_DIM)
    screenObj.refresh()


def runMonitorMode(screenObj, cliArgs):
    sharedState = SharedState(cliArgs)
    stopEvent = threading.Event()
    if not cliArgs.noHop:
        threading.Thread(target=runChannelHopper, args=(sharedState, cliArgs.interfaceName, cliArgs.channelsList, cliArgs.hopInterval, stopEvent), daemon=True).start()

    packetSniffer = AsyncSniffer(iface=cliArgs.interfaceName, prn=sharedState.handlePacket, store=False)
    packetSniffer.start()
    nextRunTime = time.time() + cliArgs.evalWindow
    isPaused = False

    try:
        while True:
            currentTime = time.time()
            if not isPaused and currentTime >= nextRunTime and not sharedState.isAnalyzing.is_set():
                with sharedState.threadLock:
                    sharedState.closeDwell()
                    windowData = sharedState.buildWindowData(currentTime - sharedState.windowStartedAt)
                    sharedState.resetWindow()
                nextRunTime = currentTime + cliArgs.evalWindow
                sharedState.isAnalyzing.set()
                threading.Thread(target=processAnalysis, args=(sharedState, windowData, cliArgs), daemon=True).start()

            renderDashboard(screenObj, sharedState, cliArgs, nextRunTime)
            screenObj.timeout(250)
            keyPress = screenObj.getch()
            if keyPress in (ord("q"), ord("Q")):
                break
            if keyPress == ord(" "):
                isPaused = not isPaused
                if isPaused:
                    nextRunTime = time.time() + cliArgs.evalWindow
    finally:
        stopEvent.set()
        try:
            packetSniffer.stop()
        except Exception:
            pass


def parseChannelsList(valStr):
    try:
        channels = [int(chNum.strip()) for chNum in valStr.split(",")]
    except Exception:
        raise argparse.ArgumentTypeError("Channels must be a comma separated list of integers")
    if not channels or any(ch < 1 or ch > 14 for ch in channels):
        raise argparse.ArgumentTypeError("Channels must be between 1 and 14")
    return channels


def main():
    cliParser = argparse.ArgumentParser(description="Wi-Fi 2.4 GHz passive RF survey instrument")
    cliParser.add_argument("--interfaceName", "-i", default="wlx00c0ca82c8b7")
    cliParser.add_argument("--channelsList", "-c", type=parseChannelsList, default=defaultChannels)
    cliParser.add_argument("--hopInterval", "-t", type=float, default=1.0)
    cliParser.add_argument("--evalWindow", "-w", type=int, default=30)
    cliParser.add_argument("--targetModel", "-m", default=defaultModel)
    cliParser.add_argument("--exportDir", default="surveyExports")
    cliParser.add_argument("--noHop", action="store_true")
    cliParser.add_argument("--noSetup", action="store_true")
    cliParser.add_argument("--restoreManaged", action="store_true")
    cliArgs = cliParser.parse_args()

    if os.geteuid() != 0:
        print("Root privileges required")
        sys.exit(1)
    if cliArgs.evalWindow < 5:
        print("Measurement window must be at least 5 seconds")
        sys.exit(1)
    if cliArgs.hopInterval <= 0:
        print("Hop interval must be greater than zero")
        sys.exit(1)
    if not cliArgs.noSetup:
        setupMonitor(cliArgs.interfaceName, cliArgs.channelsList[0])
    try:
        curses.wrapper(lambda screenObj: runMonitorMode(screenObj, cliArgs))
    finally:
        if cliArgs.restoreManaged and not cliArgs.noSetup:
            restoreManaged(cliArgs.interfaceName)


if __name__ == "__main__":
    main()
