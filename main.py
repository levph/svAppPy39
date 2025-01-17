from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from typing import Optional
from utils.fa_models import BasicSettings, PttData, RadioIP, Credentials
from utils import set_basic
from utils.get_radio_ip import sniff_target_ip
from utils.api_funcs_ss5 import list_devices, net_status, find_camera_streams_temp, get_batteries, set_ptt_groups, \
    get_basic_set
import json
from requests.exceptions import Timeout
from utils.send_commands import api_login, exit_session, set_version

app = FastAPI()

origins = [
    "http://localhost",
    "http://localhost:5173"
]

app.add_middleware(CORSMiddleware,
                   allow_origins=origins,
                   allow_credentials=True,
                   allow_methods=["*"],
                   allow_headers=["*"])
# TODO:
"""
 check encrypted/log-in protected devices - 
 can you access protected devices when passwords are different?
 or just change all function bcast calls who use other IP api?
 how does encryption affect it?
"""
# global variables
RADIO_IP = None  # string
NODE_LIST = None  # int
IP_LIST = None  # strings
VERSION = 5  # assume 5
CAM_DATA = None


@app.post("/start-up")
async def start_up():
    """
    This method is called from log-in screen. Find radio IP and whether he is protected or not.
    Updates relevant global variables.
    :return: json with type and msg fields
    """
    global RADIO_IP, NODE_LIST, IP_LIST, VERSION
    try:
        response = {"type": None, "msg": None}

        try:
            # sniff across interfaces to find Radio Discovery message
            [RADIO_IP, VERSION] = RADIO_IP if RADIO_IP else sniff_target_ip()
        except Exception as e:
            # If we got an error then most likely there's no connected Radio
            print(f"Can't find connected device\n")
            response["type"] = "Fail"
            response["msg"] = "Error when scanning net"

        else:
            if not RADIO_IP:
                print(f"No Radio connected.\n")
                response["type"] = "Fail"
                response["msg"] = "Can't find connected device"

            else:
                # if we found a device

                # get list of devices in network
                print(f"Radio IP set to {RADIO_IP}\n")
                set_version(VERSION)
                try:
                    [IP_LIST, NODE_LIST] = list_devices(RADIO_IP, VERSION)

                except (Timeout, TimeoutError):
                    print(f"Request timed out. Make sure computer/radio IP is correct")
                    response["type"] = "Fail"
                    response["msg"] = "Timeout. Incorrect computer/radio IP"

                except Exception as e:
                    if "Authentication error" in e.args[0]:
                        print(f"This device is password protected. Please log-in")
                        response["type"] = "Success"
                        response["msg"] = {"ip": RADIO_IP, "is_protected": 1}
                    else:
                        print(f"Unknown Error")
                        response["type"] = "Fail"
                        response["msg"] = "Unknown Error"
                    print(e)
                else:
                    print(f"Node List set to {NODE_LIST}")
                    print(f"IP List set to {IP_LIST}\n")

                    response["type"] = "Success"
                    response["msg"] = {"ip": RADIO_IP, "is_protected": 0}

        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/log-out")
def log_out():
    """
    Delete type of method to finish current session.
    will zeroize global variables and finish secure session if it exists
    :return: string to indicate successful exit
    """
    global RADIO_IP, NODE_LIST, IP_LIST, VERSION, CAM_DATA
    try:
        # delete current session data
        exit_session()

        # delete all global variables
        RADIO_IP = NODE_LIST = IP_LIST = VERSION = CAM_DATA = None

        return "Success"

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/log-in")
async def log_in(credentials: Credentials):
    """
    method to allow log in for locked devices
    assumes Radio IP is already set
    :param credentials: includes username and password strings
    :return:
    """
    global RADIO_IP
    try:
        username = credentials.username
        pw = credentials.password
        res = api_login(username, pw, RADIO_IP)
        if res:
            msg = "Success"
            # update variables after logging in
            _ = await start_up()
        else:
            log_out()
            msg = "Fail"
        return msg

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/set-radio-ip")
async def set_radio_ip(ip: RadioIP):
    """
    set radio ip
    :param ip:
    :return:
    """
    global RADIO_IP, NODE_LIST, IP_LIST
    try:
        # set radio IP to input ip
        RADIO_IP = ip.radio_ip

        # perform startup method (which will test connectivity and update settings)
        res = await start_up()

        if res["type"] == "Fail":
            # delete current session if wrong IP
            log_out()

        # return result which can be successful or error
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# TODO: test after encryption update
@app.get("/net-data")
async def net_data():
    """
    This method returns global variables
    assumes at least one radio is connected! will be fixed later
    :return:
    """
    global IP_LIST, NODE_LIST, VERSION

    # TODO: add labels to list
    try:
        [IP_LIST, NODE_LIST] = list_devices(RADIO_IP, VERSION)
        ip_id_dict = [{"ip": ip, "id": idd} for ip, idd in zip(IP_LIST, NODE_LIST)]
        snrs = []
        if len(IP_LIST) > 1:
            snrs = [] if len(IP_LIST) == 1 else net_status(RADIO_IP)

        msg = {
            "device-list": ip_id_dict,
            "snr-list": snrs
        }

        return json.dumps({"type": "net-data", "data": msg})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# TODO: use send_messages tools in this method
@app.post("/basic-settings")
async def basic_settings(settings: Optional[BasicSettings] = None):
    """
    :return:
    """
    try:
        if settings:
            response = set_basic.set_basic_settings(RADIO_IP, NODE_LIST, settings, VERSION)
            msg = {"Error"} if "error" in response else {"Success"}

            return msg
        else:
            response = get_basic_set(RADIO_IP)
            return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get-battery")
async def get_battery():
    """
    routine method to return battery percentage of each device in the network
    :return:
    ips_batteries - list of radio_ip - bettery status
    """
    global IP_LIST
    ips_batteries = get_batteries(IP_LIST)
    print("Updated battery status")
    print(ips_batteries)
    return json.dumps({"type": "battery", "data": ips_batteries})


async def send_messages(websocket: WebSocket, interval, func):
    """Send messages every 'interval' seconds."""
    while True:
        res = await func()
        await websocket.send_text(f"{res}")
        await asyncio.sleep(interval)


# TODO: add parameters to ws (so user could control frequency)
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    This method automates routine data sending to front
    such as battery percentages every X seconds, network SNRs, update global variables...
    :param websocket:
    :return:
    """
    await websocket.accept()
    try:

        # Create tasks for different message frequencies
        task1 = asyncio.create_task(send_messages(websocket, 300, get_battery))
        # task2 = asyncio.create_task(send_messages(websocket, 2, update_vars))
        task3 = asyncio.create_task(send_messages(websocket, 2, net_data))

        # Wait for both tasks to complete (they won't, unless there's an error)
        await asyncio.gather(task1, task3)

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await websocket.close()


@app.post("/set-ptt-groups")
async def set_ptt_group(ptt_data: PttData):
    try:
        response = set_ptt_groups(ptt_data.ips, ptt_data.num_groups, ptt_data.statuses)
        # TODO: check that response was positive
        return {"message": "ptt group settings set succesfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get-camera-links")
async def get_camera():
    # TODO: test camera_finder with cameras (connect different devices, interfaces etc...)
    """
    This endpoint will return the stream URLs of existing cameras in network
    existing URLs include main-stream, sub-stream and audio-stream
    :return:
    """
    global IP_LIST
    try:
        streams = find_camera_streams_temp(IP_LIST)
        # msg = ["Success" if res else "Failed"]
        msg = "Success"
        return {"message": msg, "data": streams}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
