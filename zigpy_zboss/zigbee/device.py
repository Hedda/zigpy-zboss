"""Zigbee device object."""
import zigpy.util
import zigpy.device
import zigpy.endpoint
import zigpy.types as t
import zigpy_zboss.types as t_nrf
from zigpy_zboss import commands as c
from zigpy.zdo import types as zdo_t
from zigpy.zdo import ZDO as ZigpyZDO


class NrfZDO(ZigpyZDO):
    """The ZDO endpoint of a device."""

    def handle_mgmt_permit_joining_req(
        self,
        permit_duration: int,
        tc_significance: int,
    ):
        """Handle ZDO permit joining request."""
        hdr = zdo_t.ZDOHeader(zdo_t.ZDOCmd.Mgmt_Permit_Joining_req, 0)
        dst_addressing = t.Addressing.IEEE

        self.listener_event("permit_duration", permit_duration)
        self.listener_event(
            "zdo_mgmt_permit_joining_req",
            self._device,
            dst_addressing,
            hdr,
            (permit_duration, tc_significance),
        )

    @zigpy.util.retryable_request
    async def Bind_req(self, eui64, ep, cluster, dst_address):
        """Binding request."""
        if dst_address.addrmode == t.Addressing.AddrMode.IEEE:
            addr_mode = t_nrf.AddressingMode.Eui64
            dst_eui64 = dst_address.ieee
        elif dst_address.addrmode == t.Addressing.AddrMode.NWK:
            addr_mode = t_nrf.AddressingMode.Nwk
            dst_eui64 = [
                dst_address.nwk % 0x100,
                dst_address.nwk >> 8,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        elif dst_address.addrmode == t.Addressing.AddrMode.Group:
            addr_mode = t_nrf.AddressingMode.Group
            dst_eui64 = [
                dst_address.nwk % 0x100,
                dst_address.nwk >> 8,
                0,
                0,
                0,
                0,
                0,
                0,
            ]

        res = await self._device._application._api.request(
            c.ZDO.BindReq.Req(
                TSN=self._device._application.get_sequence(),
                TargetNwkAddr=self._device.nwk,
                SrcIEEE=eui64,
                SrcEndpoint=ep,
                ClusterId=cluster,
                DstAddrMode=addr_mode,
                DstAddr=dst_eui64,
                DstEndpoint=dst_address.endpoint,
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode % 0xFF, dst_address, cluster)

        return (zdo_t.Status.SUCCESS, dst_address, cluster)

    @zigpy.util.retryable_request
    async def Unbind_req(self, eui64, ep, cluster, dst_address):
        """Unbinding request."""
        if dst_address.addrmode == t.Addressing.AddrMode.IEEE:
            addr_mode = t_nrf.AddressingMode.Eui64
            dst_eui64 = t.Addressing.IEEE
        elif dst_address.addrmode == t.Addressing.AddrMode.NWK:
            addr_mode = t_nrf.AddressingMode.Nwk
            dst_eui64 = [
                dst_address.nwk % 0x100,
                dst_address.nwk >> 8,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        elif dst_address.addrmode == t.Addressing.AddrMode.Group:
            addr_mode = t_nrf.AddressingMode.Group
            dst_eui64 = [
                dst_address.nwk % 0x100,
                dst_address.nwk >> 8,
                0,
                0,
                0,
                0,
                0,
                0,
            ]

        res = await self._device._application._api.request(
            c.ZDO.UnbindReq.Req(
                TSN=self._device._application.get_sequence(),
                TargetNwkAddr=self._device.nwk,
                SrcIEEE=eui64,
                SrcEndpoint=ep,
                ClusterId=cluster,
                DstAddrMode=addr_mode,
                DstAddr=dst_eui64,
                DstEndpoint=dst_address.endpoint,
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode % 0xFF, dst_address, cluster)

        return (zdo_t.Status.SUCCESS, dst_address, cluster)

    @zigpy.util.retryable_request
    def request(self, command, *args, use_ieee=False):
        """Request overwrite for Bind/Unbind requests."""
        if command == zdo_t.ZDOCmd.Bind_req:
            return self.Bind_req(*args)
        if command == zdo_t.ZDOCmd.Unbind_req:
            return self.Unbind_req(*args)
        return super().request(command, *args, use_ieee=use_ieee)

    @zigpy.util.retryable_request
    async def Node_Desc_req(self, nwk):
        """Node descriptor request."""
        res = await self._device._application._api.request(
            c.ZDO.NodeDescReq.Req(
                TSN=self._device._application.get_sequence(),
                NwkAddr=nwk
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode, None, None)

        return (zdo_t.Status.SUCCESS, None, res.NodeDesc)

    @zigpy.util.retryable_request
    async def Simple_Desc_req(self, nwk, ep):
        """Request simple descriptor."""
        res = await self._device._application._api.request(
            c.ZDO.SimpleDescriptorReq.Req(
                TSN=self._device._application.get_sequence(),
                NwkAddr=nwk,
                Endpoint=ep
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode, None, None)

        desc = zdo_t.SimpleDescriptor(
            endpoint=res.SimpleDesc.endpoint,
            profile=res.SimpleDesc.profile,
            device_type=res.SimpleDesc.device_type,
            device_version=res.SimpleDesc.device_version,
            input_clusters=res.SimpleDesc.input_clusters,
            output_clusters=res.SimpleDesc.output_clusters,
        )

        return (zdo_t.Status.SUCCESS, None, desc)

    @zigpy.util.retryable_request
    async def Active_EP_req(self, nwk):
        """Request active end points."""
        res = await self._device._application._api.request(
            c.ZDO.ActiveEpReq.Req(
                TSN=self._device._application.get_sequence(),
                NwkAddr=nwk
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode, None, None)

        return (zdo_t.Status.SUCCESS, None, res.ActiveEpList)

    @zigpy.util.retryable_request
    async def Mgmt_Lqi_req(self, idx):
        """Request Link Quality Index."""
        res = await self._device._application._api.request(
            c.ZDO.MgmtLqi.Req(
                TSN=self._device._application.get_sequence(),
                DestNWK=self._device.nwk,
                Index=idx,
            )
        )
        if res.StatusCode != 0:
            return (res.StatusCode, None)

        return (res.StatusCode, res.Neighbors)

    async def Mgmt_Leave_req(self, ieee, flags):
        """Request device leaving the network."""
        res = await self._device._application._api.request(
            c.ZDO.MgtLeave.Req(
                TSN=self._device._application.get_sequence(),
                DestNWK=t.NWK(self._device._application.devices[ieee].nwk),
                IEEE=t.EUI64(ieee),
                Flags=t.uint8_t(flags),
            )
        )
        return res.StatusCode

    async def Mgmt_Permit_Joining_req(self, duration, tc_significance):
        """Request join permition."""
        res = await self._device._application._api.request(
            c.ZDO.PermitJoin.Req(
                TSN=self._device._application.get_sequence(),
                DestNWK=t.NWK(t.BroadcastAddress.RX_ON_WHEN_IDLE),
                PermitDuration=t.uint8_t(duration),
                TCSignificance=t.uint8_t(tc_significance),
            )
        )
        return res.StatusCode

    async def Mgmt_NWK_Update_req(self, nwkUpdate):
        """Request join permition."""
        res = await self._device._application._api.request(
            c.ZDO.MgmtNwkUpdate.Req(
                TSN=self._device._application.get_sequence(),
                ScanChannelMask=nwkUpdate.ScanChannels,
                ScanDuration=nwkUpdate.ScanDuration,
                ScanCount=nwkUpdate.ScanCount,
                MgrAddr=self._device.nwk,
                DstNWK=t.NWK(0x0000),
            )
        )
        if res.StatusCode != 0:
            return (None, None, None, None, None)
        return (None, res.ScannedChannels, None, None, res.EnergyValues)


class NrfDevice(zigpy.device.Device):
    """Class representing an nRF device."""

    def __init__(self, *args, **kwargs):
        """Initialize instance."""
        super().__init__(*args, **kwargs)
        assert hasattr(self, "zdo")
        self.zdo = NrfZDO(self)
        self.endpoints[0] = self.zdo


class NrfCoordinator(NrfDevice):
    """Zigpy Device representing the controller."""

    def __init__(self, *args, **kwargs):
        """Initialize instance."""
        super().__init__(*args, **kwargs)

    @property
    def manufacturer(self):
        """Return manufacturer."""
        return "Nordic Semiconductor"

    @property
    def model(self):
        """Return model."""
        return "nRF52840"
