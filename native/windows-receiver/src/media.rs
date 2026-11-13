use crate::protocol::VideoMode;
use anyhow::{bail, Context, Result};
use std::{mem::ManuallyDrop, ptr};
use windows::{
    core::Interface,
    Win32::{
        Foundation::{BOOL, HMODULE, HWND, RECT},
        Graphics::{
            Direct3D::{D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1},
            Direct3D11::*,
            Dxgi::{Common::*, *},
        },
        Media::MediaFoundation::*,
        System::Com::{CoCreateInstance, CLSCTX_INPROC_SERVER},
        UI::WindowsAndMessaging::GetClientRect,
    },
};

struct MediaPlatform;
impl Drop for MediaPlatform {
    fn drop(&mut self) {
        unsafe {
            let _ = MFShutdown();
        }
    }
}

pub struct NativeVideo {
    transform: IMFTransform,
    manager: Option<IMFDXGIDeviceManager>,
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    video_device: ID3D11VideoDevice,
    video_context: ID3D11VideoContext,
    swap: IDXGISwapChain,
    enumerator: ID3D11VideoProcessorEnumerator,
    processor: ID3D11VideoProcessor,
    upload: Option<ID3D11Texture2D>,
    window: HWND,
    mode: VideoMode,
    output_size: (u32, u32),
    sample_index: i64,
    decoded: u64,
    pub hardware: bool,
    _platform: MediaPlatform,
}

impl NativeVideo {
    /// All COM and D3D objects stay on the owning video thread.
    pub fn new(window: HWND, mode: VideoMode) -> Result<Self> {
        unsafe {
            MFStartup(MF_VERSION, MFSTARTUP_FULL)?;
            let platform = MediaPlatform;
            let mut device = None;
            let mut context = None;
            let mut swap = None;
            let size = client_size(window)?;
            let desc = DXGI_SWAP_CHAIN_DESC {
                BufferDesc: DXGI_MODE_DESC {
                    Width: size.0,
                    Height: size.1,
                    Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                    ..Default::default()
                },
                SampleDesc: DXGI_SAMPLE_DESC {
                    Count: 1,
                    Quality: 0,
                },
                BufferUsage: DXGI_USAGE_RENDER_TARGET_OUTPUT,
                BufferCount: 2,
                OutputWindow: window,
                Windowed: BOOL(1),
                SwapEffect: DXGI_SWAP_EFFECT_FLIP_DISCARD,
                ..Default::default()
            };
            D3D11CreateDeviceAndSwapChain(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                Some(&[D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0]),
                D3D11_SDK_VERSION,
                Some(&desc),
                Some(&mut swap),
                Some(&mut device),
                None,
                Some(&mut context),
            )
            .context("Could not initialize the Windows graphics device")?;
            let device = device.context("Windows did not return a graphics device")?;
            let context = context.context("Windows did not return a graphics context")?;
            let swap = swap.context("Windows did not return a presentation surface")?;
            let video_device: ID3D11VideoDevice = device.cast()?;
            let video_context: ID3D11VideoContext = context.cast()?;
            let (enumerator, processor) = processor(&video_device, mode, size)?;
            let transform:IMFTransform=CoCreateInstance(&CMSH264DecoderMFT,None,CLSCTX_INPROC_SERVER)
                .context("Windows H.264 decoder is unavailable; install the Windows Media Feature Pack if this is Windows N")?;
            let attributes = transform.GetAttributes()?;
            attributes.SetUINT32(&MF_LOW_LATENCY, 1)?;
            let hardware = attributes.GetUINT32(&MF_SA_D3D11_AWARE).unwrap_or(0) != 0;
            let manager = if hardware {
                let mut token = 0;
                let mut manager = None;
                MFCreateDXGIDeviceManager(&mut token, &mut manager)?;
                let manager = manager.context("Windows did not return a DXGI device manager")?;
                manager.ResetDevice(&device, token)?;
                transform.ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, manager.as_raw() as usize)?;
                Some(manager)
            } else {
                None
            };
            let input = MFCreateMediaType()?;
            input.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
            input.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_H264)?;
            input.SetUINT64(&MF_MT_FRAME_SIZE, pack(mode.width, mode.height))?;
            input.SetUINT64(&MF_MT_FRAME_RATE, pack(mode.fps, 1))?;
            input.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32)?;
            transform.SetInputType(0, &input, 0)?;
            set_output(&transform)?;
            transform.ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0)?;
            transform.ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0)?;
            Ok(Self {
                transform,
                manager,
                device,
                context,
                video_device,
                video_context,
                swap,
                enumerator,
                processor,
                upload: None,
                window,
                mode,
                output_size: size,
                sample_index: 0,
                decoded: 0,
                hardware,
                _platform: platform,
            })
        }
    }

    pub fn push(&mut self, bytes: &[u8], discontinuity: bool) -> Result<u64> {
        unsafe {
            let buffer = MFCreateMemoryBuffer(bytes.len().try_into()?)?;
            let mut pointer = ptr::null_mut();
            buffer.Lock(&mut pointer, None, None)?;
            ptr::copy_nonoverlapping(bytes.as_ptr(), pointer, bytes.len());
            buffer.Unlock()?;
            buffer.SetCurrentLength(bytes.len() as u32)?;
            let sample = MFCreateSample()?;
            sample.AddBuffer(&buffer)?;
            let duration = 10_000_000 / i64::from(self.mode.fps.max(1));
            sample.SetSampleTime(self.sample_index * duration)?;
            sample.SetSampleDuration(duration)?;
            if discontinuity {
                sample.SetUINT32(&MFSampleExtension_Discontinuity, 1)?;
            }
            match self.transform.ProcessInput(0, &sample, 0) {
                Ok(()) => {}
                Err(error) if error.code() == MF_E_NOTACCEPTING => {
                    self.drain()?;
                    self.transform.ProcessInput(0, &sample, 0)?;
                }
                Err(error) => {
                    return Err(error).context("Windows could not accept an H.264 picture")
                }
            }
            self.sample_index += 1;
            self.drain()?;
            Ok(self.decoded)
        }
    }

    pub fn finish(&mut self) -> Result<u64> {
        unsafe {
            self.transform
                .ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0)?;
            self.transform
                .ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0)?;
        }
        self.drain()?;
        Ok(self.decoded)
    }

    fn drain(&mut self) -> Result<()> {
        unsafe {
            loop {
                let info = self.transform.GetOutputStreamInfo(0)?;
                let provided = info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES.0 as u32 != 0;
                let sample = if provided {
                    None
                } else {
                    let sample = MFCreateSample()?;
                    sample.AddBuffer(&MFCreateMemoryBuffer(
                        info.cbSize.max(self.mode.width * self.mode.height * 2),
                    )?)?;
                    Some(sample)
                };
                let mut output = [MFT_OUTPUT_DATA_BUFFER {
                    pSample: ManuallyDrop::new(sample),
                    ..Default::default()
                }];
                let mut flags = 0;
                let result = self.transform.ProcessOutput(0, &mut output, &mut flags);
                let sample = ManuallyDrop::take(&mut output[0].pSample);
                drop(ManuallyDrop::take(&mut output[0].pEvents));
                match result {
                    Ok(()) => {
                        if let Some(sample) = sample {
                            self.present(&sample)?;
                            self.decoded += 1;
                        } else {
                            bail!("Windows decoded no sample without asking for more input");
                        }
                    }
                    Err(error) if error.code() == MF_E_TRANSFORM_NEED_MORE_INPUT => break,
                    Err(error) if error.code() == MF_E_TRANSFORM_STREAM_CHANGE => {
                        set_output(&self.transform)?;
                    }
                    Err(error) => return Err(error).context("Windows H.264 decoding failed"),
                }
            }
        }
        Ok(())
    }

    fn present(&mut self, sample: &IMFSample) -> Result<()> {
        unsafe {
            let size = client_size(self.window)?;
            if size != self.output_size {
                self.context.ClearState();
                self.context.Flush();
                self.swap.ResizeBuffers(
                    0,
                    size.0,
                    size.1,
                    DXGI_FORMAT_UNKNOWN,
                    DXGI_SWAP_CHAIN_FLAG(0),
                )?;
                (self.enumerator, self.processor) = processor(&self.video_device, self.mode, size)?;
                self.output_size = size;
            }
            let buffer = sample.GetBufferByIndex(0)?;
            let (texture, index) = if let Ok(dxgi) = buffer.cast::<IMFDXGIBuffer>() {
                let mut raw = ptr::null_mut();
                dxgi.GetResource(&ID3D11Texture2D::IID, &mut raw)?;
                (ID3D11Texture2D::from_raw(raw), dxgi.GetSubresourceIndex()?)
            } else {
                if self.upload.is_none() {
                    let desc = D3D11_TEXTURE2D_DESC {
                        Width: self.mode.width,
                        Height: self.mode.height,
                        MipLevels: 1,
                        ArraySize: 1,
                        Format: DXGI_FORMAT_NV12,
                        SampleDesc: DXGI_SAMPLE_DESC {
                            Count: 1,
                            Quality: 0,
                        },
                        Usage: D3D11_USAGE_DEFAULT,
                        ..Default::default()
                    };
                    self.device
                        .CreateTexture2D(&desc, None, Some(&mut self.upload))?;
                }
                let mut data = ptr::null_mut();
                let mut length = 0;
                buffer.Lock(&mut data, None, Some(&mut length))?;
                let result = (|| -> Result<()> {
                    let output = self.transform.GetOutputCurrentType(0)?;
                    let stride = output
                        .GetUINT32(&MF_MT_DEFAULT_STRIDE)
                        .unwrap_or(self.mode.width);
                    if length < stride * self.mode.height * 3 / 2 {
                        bail!("Windows returned an incomplete NV12 picture");
                    }
                    self.context.UpdateSubresource(
                        self.upload.as_ref().unwrap(),
                        0,
                        None,
                        data.cast(),
                        stride,
                        length,
                    );
                    Ok(())
                })();
                buffer.Unlock()?;
                result?;
                (self.upload.as_ref().unwrap().clone(), 0)
            };
            let mut input = None;
            self.video_device.CreateVideoProcessorInputView(
                &texture,
                &self.enumerator,
                &D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
                    ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
                    Anonymous: D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC_0 {
                        Texture2D: D3D11_TEX2D_VPIV {
                            MipSlice: 0,
                            ArraySlice: index,
                        },
                    },
                    ..Default::default()
                },
                Some(&mut input),
            )?;
            let back: ID3D11Texture2D = self.swap.GetBuffer(0)?;
            let mut output = None;
            self.video_device.CreateVideoProcessorOutputView(
                &back,
                &self.enumerator,
                &D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC {
                    ViewDimension: D3D11_VPOV_DIMENSION_TEXTURE2D,
                    Anonymous: D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC_0 {
                        Texture2D: D3D11_TEX2D_VPOV { MipSlice: 0 },
                    },
                },
                Some(&mut output),
            )?;
            let output = output.context("Windows did not create a video output view")?;
            let mut render = None;
            self.device
                .CreateRenderTargetView(&back, None, Some(&mut render))?;
            self.context.ClearRenderTargetView(
                render.as_ref().context("Missing presentation view")?,
                &[0.035, 0.043, 0.063, 1.0],
            );
            let destination = fit_rect(size.0 as i32, size.1 as i32, self.mode);
            let source = RECT {
                left: 0,
                top: 0,
                right: self.mode.width as i32,
                bottom: self.mode.height as i32,
            };
            self.video_context.VideoProcessorSetStreamSourceRect(
                &self.processor,
                0,
                true,
                Some(&source),
            );
            self.video_context.VideoProcessorSetStreamDestRect(
                &self.processor,
                0,
                true,
                Some(&destination),
            );
            let mut stream = D3D11_VIDEO_PROCESSOR_STREAM {
                Enable: BOOL(1),
                pInputSurface: ManuallyDrop::new(input),
                ..Default::default()
            };
            let result = self.video_context.VideoProcessorBlt(
                &self.processor,
                &output,
                0,
                std::slice::from_ref(&stream),
            );
            drop(ManuallyDrop::take(&mut stream.pInputSurface));
            result?;
            self.swap.Present(1, DXGI_PRESENT(0)).ok()?;
        }
        Ok(())
    }
}

impl Drop for NativeVideo {
    fn drop(&mut self) {
        unsafe {
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            let _ = self
                .transform
                .ProcessMessage(MFT_MESSAGE_NOTIFY_END_STREAMING, 0);
            if self.manager.is_some() {
                let _ = self
                    .transform
                    .ProcessMessage(MFT_MESSAGE_SET_D3D_MANAGER, 0);
            }
        }
    }
}

unsafe fn set_output(transform: &IMFTransform) -> Result<()> {
    for index in 0..32 {
        let output = match transform.GetOutputAvailableType(0, index) {
            Ok(value) => value,
            Err(error) if error.code() == MF_E_NO_MORE_TYPES => break,
            Err(error) => return Err(error.into()),
        };
        if output.GetGUID(&MF_MT_SUBTYPE)? == MFVideoFormat_NV12 {
            transform.SetOutputType(0, &output, 0)?;
            return Ok(());
        }
    }
    bail!("Windows H.264 decoder did not offer NV12 output")
}

unsafe fn processor(
    device: &ID3D11VideoDevice,
    mode: VideoMode,
    size: (u32, u32),
) -> Result<(ID3D11VideoProcessorEnumerator, ID3D11VideoProcessor)> {
    let desc = D3D11_VIDEO_PROCESSOR_CONTENT_DESC {
        InputFrameFormat: D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
        InputFrameRate: DXGI_RATIONAL {
            Numerator: mode.fps,
            Denominator: 1,
        },
        InputWidth: mode.width,
        InputHeight: mode.height,
        OutputFrameRate: DXGI_RATIONAL {
            Numerator: mode.fps,
            Denominator: 1,
        },
        OutputWidth: size.0,
        OutputHeight: size.1,
        Usage: D3D11_VIDEO_USAGE_PLAYBACK_NORMAL,
    };
    let enumerator = device.CreateVideoProcessorEnumerator(&desc)?;
    let processor = device.CreateVideoProcessor(&enumerator, 0)?;
    Ok((enumerator, processor))
}

unsafe fn client_size(window: HWND) -> Result<(u32, u32)> {
    let mut rect = RECT::default();
    GetClientRect(window, &mut rect)?;
    Ok((
        (rect.right - rect.left).max(1) as u32,
        (rect.bottom - rect.top).max(1) as u32,
    ))
}

fn pack(high: u32, low: u32) -> u64 {
    (u64::from(high) << 32) | u64::from(low)
}

pub fn fit_rect(width: i32, height: i32, mode: VideoMode) -> RECT {
    let scale =
        (f64::from(width) / f64::from(mode.width)).min(f64::from(height) / f64::from(mode.height));
    let w = (f64::from(mode.width) * scale).round() as i32;
    let h = (f64::from(mode.height) * scale).round() as i32;
    RECT {
        left: (width - w) / 2,
        top: (height - h) / 2,
        right: (width + w) / 2,
        bottom: (height + h) / 2,
    }
}
