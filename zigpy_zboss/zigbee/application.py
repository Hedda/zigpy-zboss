"""ControllerApplication for ZBOSS NCP protocol based adapters."""
import asyncio
import logging
import zigpy.util
import zigpy.state
import zigpy.appdb
import zigpy.config
import zigpy.device
import async_timeout
import zigpy.endpoint
import zigpy.exceptions
import zigpy.types as t
import zigpy.application
import zigpy_zboss.types as t_nrf
import zigpy.zdo.types as zdo_t
import zigpy_zboss.config as conf

from typing import Any, Dict
from zigpy_zboss.api import NRF
from zigpy_zboss import commands as c
from zigpy.exceptions import DeliveryError
from .device import NrfCoordinator, NrfDevice
from zigpy_zboss.exceptions import NrfResponseError
from zigpy_zboss.config import CONFIG_SCHEMA, SCHEMA_DEVICE

LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT = 5
FORMAT = "%H:%M:%S"
DEVICE_JOIN_MAX_DELAY = 2
REQUEST_MAX_RETRIES = 2


class ControllerApplication(zigpy.application.ControllerApplication):
    """Controller class."""

    SCHEMA = CONFIG_SCHEMA
    SCHEMA_DEVICE = SCHEMA_DEVICE

    def __init__(self, config: Dict[str, Any]):
        """Initialize instance."""
        super().__init__(config=zigpy.config.ZIGPY_SCHEMA(config))
        self._api: NRF | None = None
        self._reset_task = None
        self.version = None

    async def connect(self):
        """Connect to the zigbee module."""
        assert self._api is None
        is_responsive = await self.probe(self.config)
        if not is_responsive:
            raise NrfResponseError
        nrf = NRF(self.config)
        await nrf.connect()
        self._api = nrf
        self._api.set_application(self)
        self._bind_callbacks()

    async def disconnect(self):
        """Disconnect from the zigbee module."""
        if self._reset_task and not self._reset_task.done():
            self._reset_task.cancel()
        if self._api is not None:
            await self._api.reset()
            self._api.close()
            self._api = None

    async def start_network(self):
        """Start the network."""
        if self.state.node_info == zigpy.state.NodeInfo():
            await self.load_network_info()

        await self.start_without_formation()

        self.version = await self._api.version()

        self.devices[self.state.node_info.ieee] = NrfCoordinator(
            self, self.state.node_info.ieee, self.state.node_info.nwk
        )

        await self._device.schedule_initialize()

    async def force_remove(self, dev: zigpy.device.Device) -> None:
        """Send a lower-level leave command to the device."""
        # ZBOSS NCP does not have any way to do this

    async def add_endpoint(self, descriptor: zdo_t.SimpleDescriptor) -> None:
        """Register a new endpoint on the device."""
        simple_desc = t_nrf.SimpleDescriptor(
            endpoint=descriptor.endpoint,
            profile=descriptor.profile,
            device_type=descriptor.device_type,
            device_version=descriptor.device_version,
            input_clusters=descriptor.input_clusters,
            output_clusters=descriptor.output_clusters,
        )
        simple_desc.input_clusters_count = len(simple_desc.input_clusters)
        simple_desc.output_clusters_count = len(simple_desc.output_clusters)
        await self._api.request(c.AF.SetSimpleDesc.Req(
            TSN=self.get_sequence(), SimpleDesc=descriptor))

    def get_sequence(self):
        """Sequence getter overwrite."""
        # Do not use tsn 255 as specified in NCP protocol.
        self._send_sequence = (self._send_sequence + 1) % 255
        return self._send_sequence

    def get_default_stack_specific_formation_settings(self):
        """Populate stack specific config dictionary with default values."""
        return {
            "rx_on_when_idle": t.Bool.true,
            "end_device_timeout": t_nrf.TimeoutIndex.Minutes_256,
            "max_children": t.uint8_t(100),
            "joined": t.Bool.false,
            "authenticated": t.Bool.false,
            "parent_nwk": None,
            "coordinator_version": None,
            "tc_policy": {
                "unique_tclk_required": t.Bool.false,
                "ic_required": t.Bool.false,
                "tc_rejoin_enabled": t.Bool.true,
                "unsecured_tc_rejoin_enabled": t.Bool.false,
                "tc_rejoin_ignored": t.Bool.false,
                "aps_insecure_join_enabled": t.Bool.false,
                "mgmt_channel_update_disabled": t.Bool.false,
            },
        }

    async def write_network_info(self, *, network_info, node_info):
        """Write the provided network and node info to the radio hardware."""
        network_info.stack_specific = \
            self.get_default_stack_specific_formation_settings()
        node_info.ieee = network_info.extended_pan_id

        # Write self.state.node_info.
        await self._api.request(
            c.NcpConfig.SetLocalIEEE.Req(
                TSN=self.get_sequence(),
                MacInterfaceNum=0,
                IEEE=node_info.ieee
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetZigbeeRole.Req(
                TSN=self.get_sequence(),
                DeviceRole=t_nrf.DeviceRole.ZC
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetExtendedPANID.Req(
                TSN=self.get_sequence(),
                ExtendedPANID=network_info.extended_pan_id
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetShortPANID.Req(
                TSN=self.get_sequence(),
                PANID=network_info.pan_id
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetChannelMask.Req(
                TSN=self.get_sequence(),
                Page=t.uint8_t(0x00),
                Mask=network_info.channel_mask
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetNwkKey.Req(
                TSN=self.get_sequence(),
                NwkKey=network_info.network_key.key,
                KeyNumber=network_info.network_key.seq
            )
        )

        # Write stack-specific parameters.
        await self._api.request(
            request=c.NcpConfig.SetRxOnWhenIdle.Req(
                TSN=self.get_sequence(),
                RxOnWhenIdle=network_info.stack_specific["rx_on_when_idle"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetEDTimeout.Req(
                TSN=self.get_sequence(),
                Timeout=network_info.stack_specific["end_device_timeout"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetMaxChildren.Req(
                TSN=self.get_sequence(),
                ChildrenNbr=network_info.stack_specific[
                    "max_children"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.TC_Link_Keys_Required,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["unique_tclk_required"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.IC_Required,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["ic_required"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.TC_Rejoin_Enabled,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["tc_rejoin_enabled"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.Ignore_TC_Rejoin,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["tc_rejoin_ignored"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.APS_Insecure_Join,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["aps_insecure_join_enabled"]
            )
        )

        await self._api.request(
            request=c.NcpConfig.SetTCPolicy.Req(
                TSN=self.get_sequence(),
                PolicyType=t_nrf.PolicyType.Disable_NWK_MGMT_Channel_Update,
                PolicyValue=network_info.stack_specific[
                    "tc_policy"]["mgmt_channel_update_disabled"]
            )
        )

        await self._form_network(network_info)

    async def _form_network(self, network_info):
        """Clear the current config and forms a new network."""
        await self._api.request(
            request=c.NWK.Formation.Req(
                TSN=self.get_sequence(),
                ChannelList=t_nrf.ChannelEntryList([
                    t_nrf.ChannelEntry(
                        page=0, channel_mask=network_info.channel_mask)
                ]),
                ScanDuration=0x05,
                DistributedNetFlag=0x00,
                DistributedNetAddr=t.NWK(0x0000),
                IEEEAddr=network_info.extended_pan_id
            )
        )

    async def load_network_info(self, *, load_devices=False):
        """Populate state.node_info and state.network_info."""
        res = await self._api.request(
            c.NcpConfig.GetJoinStatus.Req(TSN=self.get_sequence()))
        if not res.Joined & 0x01:
            raise zigpy.exceptions.NetworkNotFormed

        res = await self._api.request(c.NcpConfig.GetShortAddr.Req(
            TSN=self.get_sequence()
        ))
        self.state.node_info.nwk = res.NWKAddr

        res = await self._api.request(
            c.NcpConfig.GetLocalIEEE.Req(
                TSN=self.get_sequence(), MacInterfaceNum=0))
        self.state.node_info.ieee = res.IEEE

        res = await self._api.request(
            c.NcpConfig.GetZigbeeRole.Req(TSN=self.get_sequence()))
        self.state.node_info.logical_type = res.DeviceRole

        res = await self._api.request(
            c.NcpConfig.GetExtendedPANID.Req(TSN=self.get_sequence()))
        # FIX! Swaping bytes because of module sending IEEE the wrong way.
        self.state.network_info.extended_pan_id = t.EUI64(
            res.ExtendedPANID.serialize()[::-1])

        res = await self._api.request(
            c.NcpConfig.GetShortPANID.Req(TSN=self.get_sequence()))
        self.state.network_info.pan_id = res.PANID

        self.state.network_info.nwk_update_id = self.config[
            conf.CONF_NWK][conf.CONF_NWK_UPDATE_ID]
        self.state.network_info.nwk_manager_id = 0x0000

        res = await self._api.request(
            c.NcpConfig.GetCurrentChannel.Req(TSN=self.get_sequence()))
        self.state.network_info.channel = res.Channel

        # res = await self._api.request(
        #     c.NcpConfig.GetChannelMask.Req(TSN=self.get_sequence()))
        # self.state.network_info.channel_mask = res.ChannelList[0]

        self.state.network_info.security_level = 0x05

        res = await self._api.request(
            c.NcpConfig.GetNwkKeys.Req(TSN=self.get_sequence()))
        self.state.network_info.network_key = zigpy.state.Key(
            key=res.NwkKey1,
            tx_counter=0,
            rx_counter=0,
            seq=res.KeyNumber1,
            partner_ieee=self.state.node_info.ieee,
        )

        if self.state.node_info.logical_type == \
                zdo_t.LogicalType.Coordinator:
            self.state.network_info.tc_link_key = zigpy.state.Key(
                key=self.config[conf.CONF_NWK][conf.CONF_NWK_TC_LINK_KEY],
                tx_counter=0,
                rx_counter=0,
                seq=0,
                partner_ieee=self.state.node_info.ieee,
            )
        else:
            res = await self._api.request(
                c.NcpConfig.GetTrustCenterAddr.Req(TSN=self.get_sequence()))
            self.state.network_info.tc_link_key = (
                zigpy.state.Key(
                    key=None,
                    tx_counter=0,
                    rx_counter=0,
                    seq=0,
                    partner_ieee=res.TCIEEE,
                ),
            )

        self.state.network_info.key_table = []
        self.state.network_info.children = []
        self.state.network_info.nwk_address = {}

        res = await self._api.request(
            c.NcpConfig.GetRxOnWhenIdle.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "rx_on_when_idle"
        ] = res.RxOnWhenIdle

        res = await self._api.request(
            c.NcpConfig.GetEDTimeout.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "end_device_timeout"
        ] = res.Timeout

        res = await self._api.request(
            c.NcpConfig.GetMaxChildren.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "max_children"
        ] = res.ChildrenNbr

        res = await self._api.request(
            c.NcpConfig.GetJoinStatus.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "joined"
        ] = res.Joined

        res = await self._api.request(
            c.NcpConfig.GetAuthenticationStatus.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "authenticated"
        ] = res.Authenticated

        res = await self._api.request(
            c.NcpConfig.GetParentAddr.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "parent_nwk"
        ] = res.NWKParentAddr

        res = await self._api.request(
            c.NcpConfig.GetCoordinatorVersion.Req(TSN=self.get_sequence()))
        self.state.network_info.stack_specific[
            "coordinator_version"
        ] = res.CoordinatorVersion

        if not load_devices:
            return

    async def reset_network_info(self) -> None:
        """Reset node network information and leaves the current network."""
        pass

    async def start_without_formation(self):
        """Start the network with settings currently stored on the module."""
        res = await self._api.request(
            c.NWK.StartWithoutFormation.Req(TSN=self.get_sequence()))
        if res.StatusCode != 0:
            raise zigpy.exceptions.NetworkNotFormed

    async def permit_ncp(self, time_s=60):
        """Permits joins on the coordinator."""
        await self._api.request(
            c.ZDO.PermitJoin.Req(
                TSN=self.get_sequence(),
                DestNWK=t.NWK(0x0000),
                PermitDuration=t.uint8_t(time_s),
                TCSignificance=t.uint8_t(0x01),
            )
        )

    def permit_with_key(self, node, code, time_s=60):
        """Permit with key."""
        raise NotImplementedError

    @property
    def nrf_config(self) -> conf.ConfigType:
        """Shortcut property to access the NRF radio config."""
        return self.config[conf.CONF_NRF_CONFIG]

    @classmethod
    async def probe(cls, device_config: dict) -> bool:
        """Probe the NCP.

        Checks whether the NCP device is responding to request.
        """
        nrf = NRF(device_config)
        try:
            await nrf.connect()
            async with async_timeout.timeout(PROBE_TIMEOUT):
                await nrf.request(
                    c.NcpConfig.GetZigbeeRole.Req(TSN=1), timeout=1)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            nrf.close()

    # Overwrites zigpy because of custom ZDO layer required for ZBOSS.
    def add_device(self, ieee: t.EUI64, nwk: t.NWK):
        """Create zigpy `Device` object with the provided IEEE and NWK addr."""
        assert isinstance(ieee, t.EUI64)
        # TODO: Shut down existing device

        dev = NrfDevice(self, ieee, nwk)
        self.devices[ieee] = dev
        return dev

    #####################################################
    # Callbacks attached during startup                 #
    #####################################################

    def _bind_callbacks(self) -> None:
        """Bind indication callbacks to their associated methods."""
        self._api.register_indication_listener(
            c.NWK.NwkLeaveInd.Ind(partial=True), self.on_nwk_leave
        )
        self._api.register_indication_listener(
            c.ZDO.DevUpdateInd.Ind(partial=True), self.on_dev_update
        )
        self._api.register_indication_listener(
            c.APS.DataIndication.Ind(partial=True), self.on_apsde_indication
        )
        self._api.register_indication_listener(
            c.ZDO.DevAnnceInd.Ind(partial=True),
            self.on_zdo_device_announcement
        )
        self._api.register_indication_listener(
            c.NcpConfig.DeviceResetIndication.Ind(partial=True),
            self.on_ncp_reset
        )

    def on_nwk_leave(self, msg: c.NWK.NwkLeaveInd.Ind):
        """Device left indication."""
        dev = self.devices.get(msg.IEEE)
        if dev:
            self.handle_leave(nwk=dev.nwk, ieee=msg.IEEE)

    def on_zdo_device_announcement(self, msg: c.ZDO.DevAnnceInd.Ind):
        """ZDO Device announcement command received."""
        self.handle_join(nwk=msg.NWK, ieee=msg.IEEE, parent_nwk=None)

    def on_dev_update(self, msg: c.ZDO.DevUpdateInd.Ind):
        """Device update indication."""
        if msg.Status == t_nrf.DeviceUpdateStatus.secured_rejoin:
            # 0x000 as parent device, currently unused
            pass
            # self.handle_join(msg.Nwk, msg.IEEE, 0x0000)
        elif msg.Status == t_nrf.DeviceUpdateStatus.unsecured_join:
            # 0x000 as parent device, currently unused
            pass
            # self.handle_join(msg.Nwk, msg.IEEE, 0x0000)
        elif msg.Status == t_nrf.DeviceUpdateStatus.device_left:
            pass
            # self.handle_leave(msg.Nwk, msg.IEEE)
        elif msg.Status == t_nrf.DeviceUpdateStatus.tc_rejoin:
            pass
            # self.handle_join(msg.Nwk, msg.IEEE, 0x0000)

    def on_apsde_indication(self, msg):
        """APSDE-DATA.indication handler."""
        is_broadcast = bool(msg.FrameFC & t_nrf.APSFrameFC.Broadcast)
        is_group = bool(msg.FrameFC & t_nrf.APSFrameFC.Group)
        is_secure = bool(msg.FrameFC & t_nrf.APSFrameFC.Secure)

        if is_broadcast:
            dst = t.AddrModeAddress(
                addr_mode=t.AddrMode.Broadcast,
                address=t.BroadcastAddress.ALL_ROUTERS_AND_COORDINATOR,
            )
        elif is_group:
            dst = t.AddrModeAddress(
                addr_mode=t.AddrMode.Group,
                address=msg.GrpAddr
            )
        else:
            dst = t.AddrModeAddress(
                addr_mode=t.AddrMode.NWK,
                address=self.state.node_info.nwk,
            )

        packet = t.ZigbeePacket(
            src=t.AddrModeAddress(
                addr_mode=t.AddrMode.NWK,
                address=msg.SrcAddr,
            ),
            src_ep=msg.SrcEndpoint,
            dst=dst,
            dst_ep=msg.DstEndpoint,
            tsn=msg.Payload[1],
            profile_id=msg.ProfileId,
            cluster_id=msg.ClusterId,
            data=t.SerializableBytes(
                t.List[t.uint8_t](msg.Payload[0:msg.PayloadLength]).serialize()
            ),
            tx_options=(
                t.TransmitOptions.APS_Encryption
                if is_secure
                else t.TransmitOptions.NONE
            ),
        )

        self.packet_received(packet)

    def on_ncp_reset(self, msg):
        """NCP_RESET.indication handler."""
        if msg.ResetSrc == t_nrf.ResetSource.RESET_SRC_POWER_ON:
            return
        LOGGER.debug(
            f"Resetting ControllerApplication. Source: {msg.ResetSrc}")
        if self._reset_task:
            LOGGER.debug("Preempting ControllerApplication reset")
            self._reset_task.cancel()

        self._reset_task = asyncio.create_task(self._reset_controller())

    async def _reset_controller(self):
        """Restart the application controller."""
        self.disconnect()
        await self.startup()

    async def send_packet(self, packet: t.ZigbeePacket) -> None:
        """Send packets."""
        if self._api is None:
            raise DeliveryError(
                "Coordinator is disconnected, cannot send request")

        LOGGER.debug("Sending packet %r", packet)

        options = c.aps.TransmitOptions.NONE

        if t.TransmitOptions.ACK in packet.tx_options:
            options |= c.aps.TransmitOptions.ACK_ENABLED

        if t.TransmitOptions.APS_Encryption in packet.tx_options:
            options |= c.aps.TransmitOptions.SECURITY_ENABLED

        # Prepare ZBOSS types from zigpy types.
        dst_addr = packet.dst.address
        dst_addr_mode = packet.dst.addr_mode
        if packet.dst.addr_mode != t.AddrMode.IEEE:
            dst_addr = t.EUI64(
                [
                    packet.dst.address % 0x100,
                    packet.dst.address >> 8,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ]
            )
        if packet.dst.addr_mode == t.AddrMode.Broadcast:
            dst_addr_mode = t.AddrMode.Group

        # Don't release the concurrency-limiting semaphore until we are done
        # trying. There is no point in allowing requests to take turns getting
        # buffer errors.
        async with self._limit_concurrency():
            await self._api.request(
                c.APS.DataReq.Req(
                    TSN=packet.tsn,
                    ParamLength=t.uint8_t(21),  # Fixed value 21
                    DataLength=t.uint16_t(len(packet.data.serialize())),
                    DstAddr=dst_addr,
                    ProfileID=packet.profile_id,
                    ClusterId=packet.cluster_id,
                    DstEndpoint=packet.dst_ep,
                    SrcEndpoint=packet.src_ep,
                    Radius=packet.radius or 0,
                    DstAddrMode=dst_addr_mode,
                    TxOptions=options,
                    UseAlias=t.Bool.false,
                    AliasSrcAddr=t.NWK(0x0000),
                    AliasSeqNbr=t.uint8_t(0x00),
                    Payload=t_nrf.Payload(packet.data.serialize()),
                )
            )
